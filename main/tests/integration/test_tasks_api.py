"""任务 API 集成测试（迁移 schema + HTTP + 显式 executor 扫描）。

后台循环间隔拉大到 3600s 隔离（测试不依赖 lifespan 循环，轮询测试显式调
executor.scan_once——V3A 同款"显式 scan_once"模式）；种子直写迁移后 DB
（FK 强制：users 前置 + ApiKey 用户域种子）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Chapter, LearningProject, Material, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import scan_once as scan_tasks
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

# V5A executor 解密路径：种子写入真实加密 Key；scan_tasks 注入 mock transport（不触网）
# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport 全链路分派（LLM 升级管线）：<PLANNER_INPUT> → 按请求配额产出
    锚定单元（引用请求内组页）；<SCORING_INPUT> → ID 守恒的确定性分数；其余
    （<GENERATION_SPEC>）→ 每批 1 张合法卡（1 单元 1 批）。COMPACT 2 章 = 6 单元
    → 6 批 → 6 卡。"""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        if "<PLANNER_INPUT>" in user:
            payload = json.loads(
                user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0]
            )
            chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
            units: list[dict[str, object]] = []
            # 难度键原样回显（与 test_observability 同款）：planner 输出 schema v3
            # 枚举为 BASIC/UNDERSTANDING/DEEP_QUESTION 且单元必填 coverage_tier
            # （Task 7 资产 v4/v3 起服务端配额键与模型输出口径一致）
            for difficulty, quota_i in payload["difficulty_interval"].items():
                for _ in range(quota_i["max"]):
                    units.append(
                        {
                            "source_chunk_ids": [chunk_ids[0]],
                            "learning_objective": f"知识点{len(units)}",
                            "target_difficulty": difficulty,
                            "card_type": "QUESTION",
                            "coverage_tier": "CORE",
                        }
                    )
            content = json.dumps({"units": units}, ensure_ascii=False)
        elif "<SCORING_INPUT>" in user:
            payload = json.loads(
                user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0]
            )
            content = json.dumps(
                {
                    "scores": [
                        {
                            "generation_item_id": item["generation_item_id"],
                            "evidence_score": 2,
                            "correctness_score": 3,
                            "difficulty_score": 2,
                            "learning_value_score": 2,
                        }
                        for item in payload["items"]
                    ]
                },
                ensure_ascii=False,
            )
        else:  # 生成调用：1 单元 1 批 → 每批 1 张合法卡
            content = json.dumps(
                {"cards": [{"type": "QUESTION", "question": "q0", "answer": "a0"}]},
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "tasks_api.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # Bearer 注册请求计入 IP 维度（连发 >5 req/s），显式调高隔离,
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _user(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def _seed_context(db_path: Path, *, user_id: str, with_key: bool = True) -> dict[str, object]:
    """users 前置 + PDF + 2 章节 + 牌组 + ApiKey（tasks 创建校验 Key）。

    PDF/牌组 user 域（tasks 归属校验）；ApiKey 用户域（P4-4 起 Key 归属切 user 域——
    Core 直写只写所需列）。
    """
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用
            session.add(
                User(
                    user_id=user_id,
                    username=f"u-{user_id[:8]}",
                    email=f"u-{user_id[:8]}@example.com",
                    password_hash="x",
                    created_at="2026-08-11T00:00:00.000Z",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
        pdf = PdfFile(
            file_id=_uuid(),
            user_id=user_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        project = LearningProject(
            project_id=_uuid(),
            user_id=user_id,
            name="P",
            chapters_confirmed_at="2026-08-11T00:00:00.000Z",
            version="2026-08-11T00:00:00.000Z",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(project)
        session.flush()
        session.add(
            Material(
                material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=project.project_id,
                type="PDF",
                name="seed.pdf",
                status=None,
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
        deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
        session.flush()
        chapter_ids: list[str] = []
        for i in range(2):
            ch = Chapter(
                chapter_id=_uuid(),
                file_id=pdf.file_id,
                material_id=pdf.file_id,
                name=f"第{i + 1}章",
                start_page=i + 1,
                end_page=i + 2,
            )
            session.add(ch)
            session.flush()
            chapter_ids.append(ch.chapter_id)
        if with_key:
            session.execute(
                insert(ApiKey).values(
                    user_id=user_id,
                    encrypted_key=_ENCRYPTED_TEST_KEY,
                    status="AVAILABLE",
                    masked_key="sk-****",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
            session.flush()
        # LLM 升级管线：规划 worker 读取章节范围内页文本（text_chunks）——
        # 缺页文本则规划空结果（NO_GENERATION_UNITS），轮询测试需真实页文本
        persist_text_chunks(
            session,
            file_id=pdf.file_id,
            pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    return {
        "project_id": project.project_id,
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
    }


def _payload(seed: dict[str, object], *, tendency: str = "COMPACT") -> dict[str, object]:
    """V2.5 请求体（project_id 取自路径；file_id 经 query 过渡参数传入）。"""
    return {
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "coverage_mode": tendency,
            "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
        },
    }


def _post_task(
    client: TestClient,
    seed: dict[str, object],
    user: dict[str, str],
    idem: dict[str, str] | None = None,
) -> httpx.Response:
    """POST /projects/{project_id}/tasks（V2.5 4.3：项目归属入口）。"""
    headers = {**user, **(idem or _idem())}
    return cast(
        httpx.Response,
        client.post(f"/projects/{seed['project_id']}/tasks", json=_payload(seed), headers=headers),
    )


def test_tasks_create_201_draft_with_chapter_snapshot(ctx: tuple[TestClient, Path]) -> None:
    """POST /projects/{id}/tasks → 201 DRAFT（V2.5 4.1/6.4 自动保存：创建即 DRAFT，
    不规划）；selected_chapters 为 Chapter 对象数组快照（契约 3.4）。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    resp = _post_task(client, seed, user)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["internal_stage"] is None  # start 后才有内部阶段（PLANNING→…）
    assert body["generated_card_count"] == 0
    chapters = body["selected_chapters"]
    assert len(chapters) == 2
    assert set(chapters[0]) == {"chapter_id", "material_id", "name", "start_page", "end_page"}
    assert chapters[0]["name"] == "第1章"
    assert body["generation_config"]["coverage_mode"] == "COMPACT"  # V2.5 改名
    assert body["resumable"] is False


def test_tasks_create_missing_idempotency_key_400(ctx: tuple[TestClient, Path]) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）：缺失 → 400 VALIDATION_ERROR。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    resp = client.post(f"/projects/{seed['project_id']}/tasks", json=_payload(seed), headers=user)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_tasks_create_without_api_key_422(ctx: tuple[TestClient, Path]) -> None:
    """未保存可用 API Key → 422 API_KEY_NOT_SET（6.2）。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path), with_key=False)
    resp = _post_task(client, seed, user)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "API_KEY_NOT_SET"


def test_tasks_create_idempotent_replay(ctx: tuple[TestClient, Path]) -> None:
    """同 key 同 body 重放：返回首次响应，任务只创建一次。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    headers = {**user, **_idem()}
    payload = _payload(seed)
    path = f"/projects/{seed['project_id']}/tasks"
    r1 = client.post(path, json=payload, headers=headers)
    r2 = client.post(path, json=payload, headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        rows = session.scalars(select(Task)).all()
    assert len(rows) == 1  # 幂等重放不重复创建


def test_tasks_create_idempotency_conflict_409(ctx: tuple[TestClient, Path]) -> None:
    """同 key 异 body → 409 IDEMPOTENCY_CONFLICT。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    headers = {**user, **_idem()}
    path = f"/projects/{seed['project_id']}/tasks"
    assert client.post(path, json=_payload(seed), headers=headers).status_code == 201
    resp = client.post(
        path,
        json=_payload(seed, tendency="EXTENSIVE"),
        headers=headers,
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_tasks_get_polls_until_completed(ctx: tuple[TestClient, Path]) -> None:
    """长任务轮询（V2.5 完整流程）：创建 DRAFT → 请求样卡 → 显式扫描（样卡 worker
    完成）→ start → 显式扫描（规划/生成/评分衔接）→ GET 返回 COMPLETED。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    resp = _post_task(client, seed, user)
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    # 请求样卡（DRAFT → SAMPLE_GENERATING）→ 样卡 worker 后台完成 → AWAITING
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
    resp = client.get(f"/tasks/{task_id}", headers=user)
    assert resp.status_code == 200
    assert resp.json()["status"] == "AWAITING_SAMPLE_CONFIRMATION"
    # start（校验样卡 hash）→ 规划/生成/评分 worker → COMPLETED
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    final: dict[str, object] = {}
    for _ in range(10):
        scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
        resp = client.get(f"/tasks/{task_id}", headers=user)
        assert resp.status_code == 200
        final = resp.json()
        if final["status"] == "COMPLETED":
            break
    assert final["status"] == "COMPLETED"
    # COMPACT 2 章确定性 6 卡：mock planner 按请求配额产出 6 单元 → 6 批 → 每批 1 卡
    # （_client_factory docstring；配额 BASIC 3/UNDERSTANDING 2/DEEP_QUESTION 1）
    assert final["generated_card_count"] == 6
    assert final["ended_at"] is not None
    assert final["resumable"] is False
