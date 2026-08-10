"""任务 service 集成测试：创建/状态机/取消/resume/校验（真实 SQLite + fake 执行）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
pdf/deck/task/api_keys 均 FK → devices——fixture 先建设备行
（HTTP 流中由 F1 设备中间件自动建立，本层显式补种）；tasks 校验 Key 需 ApiKey 种子。
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, Base, Chapter, Device, KnowledgePoint, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.tasks.service import cancel_task, create_task, get_task, resume_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_context(session: Session, *, device_id: str, with_key: bool = True) -> dict[str, Any]:
    """devices 前置 + PDF/章节/牌组 + ApiKey 种子（tasks 校验 Key）。"""
    session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    chapter_ids = []
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
                encrypted_key="enc",
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
    session.flush()
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _config(tendency: str = "BALANCED") -> dict[str, str | dict[str, float]]:
    return {
        "quantity_tendency": tendency,
        "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
    }


def test_tasks_create_runs_and_plans(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            device_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        task_id = task.task_id
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        status = task.status
    assert status == "RUNNING"
    assert len(kps) > 0
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.generation_config  # JSON 快照持久化


def test_tasks_create_without_key_422(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device, with_key=False)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            device_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET


def test_tasks_create_cross_device_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            device_id=_uuid(),
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_tasks_get_missing_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_task(session, device_id=device, task_id=_uuid())
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_tasks_cancel_keeps_cards(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            device_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        task_id = task.task_id
        result = cancel_task(
            session, device_id=device, task_id=task_id, now="2026-08-11T01:00:00.000Z"
        )
        session.commit()
    assert result.status == "CANCELLED"


def test_tasks_resume_paused(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            device_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.flush()
        task.status = "PAUSED"
        task.resumable = 1
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = resume_task(
            session, device_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z"
        )
        session.commit()
    assert result.status == "RUNNING"
    # 再 resume（RUNNING 非 PAUSED）→ 409
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        resume_task(session, device_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
