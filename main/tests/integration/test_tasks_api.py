"""任务 API 集成测试（迁移 schema + HTTP + 显式 executor 扫描）。

后台循环间隔拉大到 3600s 隔离（测试不依赖 lifespan 循环，轮询测试显式调
executor.scan_once——V3A 同款"显式 scan_once"模式）；种子直写迁移后 DB
（FK 强制：devices 前置 + ApiKey 种子）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Chapter, Device, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import scan_once as scan_tasks

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

# V5A executor 解密路径：种子写入真实加密 Key；scan_tasks 注入 mock transport（不触网）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport 全链路分派（LLM 升级管线）：<PLANNER_INPUT> → 按请求配额产出
    锚定单元（引用请求内组页）；<SCORING_INPUT> → ID 守恒的确定性分数；其余
    （<GENERATOR_INPUT>）→ 每批 1 张合法卡（1 单元 1 批）。COMPACT 2 章 = 6 单元
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
            for difficulty, quota in payload["difficulty_quota"].items():
                for _ in range(quota):
                    units.append(
                        {
                            "source_chunk_ids": [chunk_ids[0]],
                            "learning_objective": f"知识点{len(units)}",
                            "target_difficulty": difficulty,
                            "card_type": "QUESTION",
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
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_context(db_path: Path, *, device_id: str, with_key: bool = True) -> dict[str, object]:
    """devices 前置 + PDF + 2 章节 + 牌组 + ApiKey（tasks 创建校验 Key）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            device_id=device_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        chapter_ids: list[str] = []
        for i in range(2):
            ch = Chapter(
                chapter_id=_uuid(),
                file_id=pdf.file_id,
                name=f"第{i + 1}章",
                start_page=i + 1,
                end_page=i + 2,
            )
            session.add(ch)
            session.flush()
            chapter_ids.append(ch.chapter_id)
        if with_key:
            session.add(
                ApiKey(
                    device_id=device_id,
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
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _payload(seed: dict[str, object], *, tendency: str = "COMPACT") -> dict[str, object]:
    return {
        "file_id": seed["file_id"],
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "quantity_tendency": tendency,
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
    }


def test_tasks_create_201_pending_with_chapter_snapshot(ctx: tuple[TestClient, Path]) -> None:
    """POST /tasks → 201 PENDING+PLANNING（T8 新语义：创建不自动规划，规划 worker
    CAS 接管）；selected_chapters 为 Chapter 对象数组快照（契约 3.4）。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["stage"] == "PLANNING"
    assert body["generated_card_count"] == 0
    chapters = body["selected_chapters"]
    assert len(chapters) == 2
    assert set(chapters[0]) == {"chapter_id", "name", "start_page", "end_page"}
    assert chapters[0]["name"] == "第1章"
    assert body["generation_config"]["quantity_tendency"] == "COMPACT"
    assert body["resumable"] is False


def test_tasks_create_missing_idempotency_key_400(ctx: tuple[TestClient, Path]) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）：缺失 → 400 VALIDATION_ERROR。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers=device)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_tasks_create_without_api_key_422(ctx: tuple[TestClient, Path]) -> None:
    """未保存可用 API Key → 422 API_KEY_NOT_SET（6.2）。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"], with_key=False)
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "API_KEY_NOT_SET"


def test_tasks_create_idempotent_replay(ctx: tuple[TestClient, Path]) -> None:
    """同 key 同 body 重放：返回首次响应，任务只创建一次。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    headers = {**device, **_idem()}
    payload = _payload(seed)
    r1 = client.post("/tasks", json=payload, headers=headers)
    r2 = client.post("/tasks", json=payload, headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        rows = session.scalars(select(Task)).all()
    assert len(rows) == 1  # 幂等重放不重复创建


def test_tasks_create_idempotency_conflict_409(ctx: tuple[TestClient, Path]) -> None:
    """同 key 异 body → 409 IDEMPOTENCY_CONFLICT。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    headers = {**device, **_idem()}
    assert client.post("/tasks", json=_payload(seed), headers=headers).status_code == 201
    resp = client.post("/tasks", json=_payload(seed, tendency="EXTENSIVE"), headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_tasks_get_polls_until_completed(ctx: tuple[TestClient, Path]) -> None:
    """长任务轮询：显式 executor 扫描后 GET 返回 COMPLETED（COMPACT=3 知识点/章 × 2 章）。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    final: dict[str, object] = {}
    for _ in range(10):
        scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
        resp = client.get(f"/tasks/{task_id}", headers=device)
        assert resp.status_code == 200
        final = resp.json()
        if final["status"] == "COMPLETED":
            break
    assert final["status"] == "COMPLETED"
    assert final["generated_card_count"] == 6  # COMPACT 2 章 → 6 单元 → 6 批 × 每批 1 卡
    assert final["ended_at"] is not None
    assert final["resumable"] is False


def test_tasks_cancel_200(ctx: tuple[TestClient, Path]) -> None:
    """POST cancel → 200 CANCELLED（已入库卡片保留，V4 取消时无卡片）。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    resp = client.post(f"/tasks/{task_id}/cancel", headers={**device, **_idem()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["ended_at"] is not None
    # 终态再取消保持 CANCELLED（状态机不变式）
    resp = client.post(f"/tasks/{task_id}/cancel", headers={**device, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "CANCELLED"


def test_tasks_resume_200_then_409(ctx: tuple[TestClient, Path]) -> None:
    """PAUSED+resumable=1 → resume 200 RUNNING；再 resume（RUNNING）→ 409 TASK_STATE_CONFLICT。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    # 直写 PAUSED + resumable=1（服务端无 PAUSED 入口；后台循环间隔 3600 不干预）
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.status = "PAUSED"
        task.resumable = 1
        session.commit()
    resp = client.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "RUNNING"
    resp = client.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_STATE_CONFLICT"
