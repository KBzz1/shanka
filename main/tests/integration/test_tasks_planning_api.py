"""任务创建改造 API 测试（spec §6.1/§10；Task 8）：PENDING+PLANNING 视图 + 预算上限 400。

- brief 指定 `tests/app/api/test_tasks_planning_api.py`，仓库无 tests/app/ 目录，
  按仓库约定放 tests/integration/（test_tasks_api.py 同款基座：迁移 schema +
  TestClient + 种子直写；adaptation 见任务报告）；
- 预算超限用例：Settings(max_generation_units_per_task=5) + 5 章 COMPACT（预算 15）
  → POST /tasks 400 VALIDATION_ERROR，不创建任务。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Chapter, Device, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

_NOW = "2026-08-12T00:00:00.000Z"


def _make_client(tmp_path: Path, *, max_units: int) -> tuple[TestClient, Path]:
    """迁移后 schema 的 TestClient（后台循环隔离：间隔 3600s）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "tasks_planning_api.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环
        max_generation_units_per_task=max_units,
    )
    return TestClient(create_app(settings)), db_path


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    client, db_path = _make_client(tmp_path, max_units=300)
    with client:
        yield client, db_path


@pytest.fixture
def ctx_strict(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """预算上限收紧到 5：5 章 COMPACT=15 > 5 → 400。"""
    client, db_path = _make_client(tmp_path, max_units=5)
    with client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_context(db_path: Path, *, device_id: str, chapter_count: int = 2) -> dict[str, object]:
    """devices 前置 + PDF + chapter_count 章节 + 牌组 + ApiKey（tasks 创建校验 Key）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        session.add(Device(device_id=device_id, created_at=_NOW))
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            device_id=device_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at=_NOW,
        )
        session.add(pdf)
        session.flush()
        deck = create_deck(session, device_id=device_id, name="D", now=_NOW)
        session.flush()
        chapter_ids: list[str] = []
        for i in range(chapter_count):
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
        session.add(
            ApiKey(
                device_id=device_id,
                encrypted_key="enc",
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        session.flush()
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


def test_tasks_create_201_pending_planning_view(ctx: tuple[TestClient, Path]) -> None:
    """POST /tasks → 201 PENDING+PLANNING：started_at/total_batch_count 为空、快照完整、新字段。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "PENDING"
    assert body["stage"] == "PLANNING"
    assert body["started_at"] is None
    assert body["total_batch_count"] is None
    assert body["completion_reason"] is None
    assert body["skipped_planning_group_count"] == 0
    chapters = body["selected_chapters"]
    assert len(chapters) == 2
    assert set(chapters[0]) == {"chapter_id", "name", "start_page", "end_page"}
    assert chapters[0]["name"] == "第1章"
    assert body["generation_config"]["quantity_tendency"] == "COMPACT"
    assert body["resumable"] is False


def test_tasks_create_budget_exceeded_400(ctx_strict: tuple[TestClient, Path]) -> None:
    """预算超上限（5 章 COMPACT=15 > 5）→ 400 VALIDATION_ERROR，不创建任务（spec §10）。"""
    client, db_path = ctx_strict
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"], chapter_count=5)
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "生成单元预算超出上限"
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        rows = session.scalars(select(Task)).all()
    assert rows == []
