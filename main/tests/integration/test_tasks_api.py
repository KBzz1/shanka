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
    """长任务轮询（V2.5 完整流程，确认闭环）：创建 DRAFT → 请求样卡 → 显式扫描（样卡
    worker 完成）→ start → 显式扫描（规划/生成/评分衔接）→ park 至
    AWAITING_CONFIRMATION → POST confirm（幂等键）→ GET 返回 COMPLETED。"""
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
    # start（校验样卡 hash）→ 规划/生成/评分 worker → park 至 AWAITING_CONFIRMATION
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    parked: dict[str, object] = {}
    for _ in range(10):
        scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
        resp = client.get(f"/tasks/{task_id}", headers=user)
        assert resp.status_code == 200
        parked = resp.json()
        if parked["status"] == "AWAITING_CONFIRMATION":
            break
    assert parked["status"] == "AWAITING_CONFIRMATION"
    assert parked["internal_stage"] is None
    assert parked["ended_at"] is None  # park 不写 ended_at（confirm 时点写入）
    # 用户确认发布 → COMPLETED（单事务整批 STAGED → PUBLISHED）
    resp = client.post(f"/tasks/{task_id}/confirm", headers={**user, **_idem()})
    assert resp.status_code == 200
    final = resp.json()
    assert final["status"] == "COMPLETED"
    # COMPACT 2 章确定性 6 卡：mock planner 按请求配额产出 6 单元 → 6 批 → 每批 1 卡
    # （_client_factory docstring；配额 BASIC 3/UNDERSTANDING 2/DEEP_QUESTION 1）
    assert final["generated_card_count"] == 6
    assert final["ended_at"] is not None
    assert final["resumable"] is False


# ---------- 4.1 确认闭环：park / confirm / retry-supersede / 复审读出口 ----------


def _drive_to_parked(
    client: TestClient, db_path: Path, user: dict[str, str], seed: dict[str, object]
) -> str:
    """完整推进至 park：创建 → 样卡 → start → 扫描排空 → AWAITING_CONFIRMATION。"""
    resp = _post_task(client, seed, user)
    assert resp.status_code == 201
    task_id = str(resp.json()["task_id"])
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    for _ in range(10):
        if scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory) == 0:
            break
    body = client.get(f"/tasks/{task_id}", headers=user).json()
    assert body["status"] == "AWAITING_CONFIRMATION", f"未收敛 park 态: {body['status']}"
    return task_id


def test_tasks_confirm_publishes_and_unlocks_scheduling(ctx: tuple[TestClient, Path]) -> None:
    """4.1 确认闭环可见性（交接 §11 后端验收 1）：park 后卡对 decks/study 全不可见、
    卡组计数 0、不可入学习计划；confirm 后单事务全部可见且可入计划。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    deck_id = str(seed["deck_id"])
    project_id = str(seed["project_id"])
    task_id = _drive_to_parked(client, db_path, user, seed)

    # park 期间：STAGED 对一切用户读不可见；复审读出口是唯一例外
    assert client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"] == []
    decks = client.get("/decks", headers=user).json()["items"]
    parked_deck = next(d for d in decks if d["deck_id"] == deck_id)
    assert parked_deck["card_count"] == 0
    review = client.get(f"/tasks/{task_id}/cards", headers=user).json()["items"]
    assert len(review) == 6
    assert all(c["publication_state"] == "STAGED" for c in review)
    assert [c["position"] for c in review] == sorted(c["position"] for c in review)
    # park 时 operation 保持 ACTIVE（非终态——终结点在 confirm/retry/资源删除）
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        op_status, op_reason = conn.execute(
            text("SELECT status, terminal_reason FROM generation_operations WHERE task_id = :t"),
            {"t": task_id},
        ).one()
    assert op_status == "ACTIVE" and op_reason is None
    # 计划守卫（Figma 1019-5568 语义）：仅含待确认卡的卡组不可加入学习计划
    plan_payload = {
        "project_id": project_id,
        "selected_deck_ids": [deck_id],
        "daily_new_goal": 20,
        "daily_review_goal": 30,
    }
    resp = client.put("/study/plan", json=plan_payload, headers={**user, **_idem()})
    assert resp.status_code == 400
    assert "暂无可学习卡片" in resp.json()["error"]["message"]

    # confirm → COMPLETED：单事务发布，卡组计数/可见卡/计划守卫全部恢复
    resp = client.post(f"/tasks/{task_id}/confirm", headers={**user, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "COMPLETED"
    assert resp.json()["ended_at"] is not None
    with engine.connect() as conn:
        op_status, op_reason = conn.execute(
            text("SELECT status, terminal_reason FROM generation_operations WHERE task_id = :t"),
            {"t": task_id},
        ).one()
    assert op_status == "COMPLETED" and op_reason == "USER_CONFIRMED"
    assert len(client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"]) == 6
    decks = client.get("/decks", headers=user).json()["items"]
    assert next(d for d in decks if d["deck_id"] == deck_id)["card_count"] == 6
    resp = client.put("/study/plan", json=plan_payload, headers={**user, **_idem()})
    assert resp.status_code == 200
    # 确认后复审读出口关闭（确认后走 GET /decks/{deck_id}/cards）
    assert client.get(f"/tasks/{task_id}/cards", headers=user).status_code == 409


def test_tasks_confirm_idempotency_and_conflict(ctx: tuple[TestClient, Path]) -> None:
    """confirm 并发规则：同键同体重放首次 200；异键重复确认 409；确认后 retry 409。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _drive_to_parked(client, db_path, user, seed)
    key = _idem()
    first = client.post(f"/tasks/{task_id}/confirm", headers={**user, **key})
    assert first.status_code == 200
    replay = client.post(f"/tasks/{task_id}/confirm", headers={**user, **key})
    assert replay.status_code == 200  # 同键重放（幂等层）
    conflict = client.post(f"/tasks/{task_id}/confirm", headers={**user, **_idem()})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "TASK_STATE_CONFLICT"
    # 确认后不可重新生成（保护学习记录）
    retry = client.post(f"/tasks/{task_id}/retry", headers={**user, **_idem()})
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "TASK_STATE_CONFLICT"


def test_tasks_confirm_state_guards(ctx: tuple[TestClient, Path]) -> None:
    """confirm 前置：非待确认任务 409；跨用户 404（不暴露存在性）。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    resp = _post_task(client, seed, user)
    assert resp.status_code == 201
    draft_task_id = resp.json()["task_id"]
    assert (
        client.post(f"/tasks/{draft_task_id}/confirm", headers={**user, **_idem()}).status_code
        == 409
    )
    other = auth_headers(client, username="bob")
    assert (
        client.post(f"/tasks/{draft_task_id}/confirm", headers={**other, **_idem()}).status_code
        == 404
    )
    assert client.get(f"/tasks/{draft_task_id}/cards", headers=other).status_code == 404


def test_tasks_retry_supersedes_awaiting_confirmation(ctx: tuple[TestClient, Path]) -> None:
    """确认前重新生成（4.1 supersede 单事务）：原任务 ABANDONED/SUPERSEDED + STAGED 卡
    硬删（无学习记录可丢）+ 新 DRAFT 任务（同配置快照、不携带样卡）；幂等重放不双建。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    deck_id = str(seed["deck_id"])
    task_id = _drive_to_parked(client, db_path, user, seed)

    key = _idem()
    resp = client.post(f"/tasks/{task_id}/retry", headers={**user, **key})
    assert resp.status_code == 201
    new_task = resp.json()
    assert new_task["status"] == "DRAFT"  # 重走样卡流程
    assert new_task["retry_of_task_id"] == task_id
    assert new_task["sample_cards"] is None  # 不携带样卡
    assert new_task["deck_id"] == deck_id

    # 原任务：ABANDONED + completion_reason=SUPERSEDED + ended_at；STAGED 卡硬删
    original = client.get(f"/tasks/{task_id}", headers=user).json()
    assert original["status"] == "ABANDONED"
    assert original["completion_reason"] == "SUPERSEDED"
    assert original["ended_at"] is not None
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        staged_count = conn.execute(
            text("SELECT COUNT(*) FROM cards WHERE source_task_id = :t"), {"t": task_id}
        ).scalar_one()
    assert staged_count == 0  # STAGED 卡硬删（此时不可能有学习记录）
    # 幂等重放：同 operation key 返回同一新任务，不双建
    replay = client.post(f"/tasks/{task_id}/retry", headers={**user, **key})
    assert replay.status_code == 201
    assert replay.json()["task_id"] == new_task["task_id"]
    with engine.connect() as conn:
        task_rows = conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()
    assert task_rows == 2  # 原 + 新，重放不双建


def test_tasks_list_deck_id_filter(ctx: tuple[TestClient, Path]) -> None:
    """GET /tasks?deck_id= 过滤（前端卡组四态渲染）；无 deck_id 行为不变。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    deck_id = str(seed["deck_id"])
    # deck A：完整推进至 park；deck B：仅 DRAFT
    parked_task_id = _drive_to_parked(client, db_path, user, seed)
    with task_factory() as session:
        other_deck = create_deck(
            session,
            user_id=_user_id(db_path),
            name="B",
            now="2026-08-15T00:00:00.000Z",
            project_id=str(seed["project_id"]),
        )
        session.commit()
        other_deck_id = other_deck.deck_id
    resp = _post_task(client, {**seed, "deck_id": other_deck_id}, user)
    assert resp.status_code == 201
    draft_task_id = resp.json()["task_id"]

    by_deck = client.get(f"/tasks?deck_id={deck_id}", headers=user).json()["items"]
    assert [t["task_id"] for t in by_deck] == [parked_task_id]
    by_other = client.get(f"/tasks?deck_id={other_deck_id}", headers=user).json()["items"]
    assert [t["task_id"] for t in by_other] == [draft_task_id]
    all_tasks = client.get("/tasks", headers=user).json()["items"]
    assert {t["task_id"] for t in all_tasks} == {parked_task_id, draft_task_id}
    # deck 过滤与 status 过滤可组合
    parked_only = client.get(
        f"/tasks?deck_id={deck_id}&status=AWAITING_CONFIRMATION", headers=user
    ).json()["items"]
    assert [t["task_id"] for t in parked_only] == [parked_task_id]
