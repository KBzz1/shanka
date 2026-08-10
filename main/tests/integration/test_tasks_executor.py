"""任务执行器集成测试：fake 生成入库/状态机/防重。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import Base, Card, KnowledgePoint, Task
from infra.db.session import create_db_engine, create_session_factory
from services.tasks.executor import process_running_tasks
from services.tasks.service import create_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'exec.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(session: Session, *, device_id: str) -> str:
    from infra.db.models import ApiKey, Chapter, Device, PdfFile
    from services.decks.service import create_deck

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
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
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
    task = create_task(
        session,
        device_id=device_id,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config={
            "quantity_tendency": "COMPACT",
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
        now="2026-08-11T00:00:00.000Z",
    )
    session.commit()
    return task.task_id


def test_executor_completes_task_and_inserts_cards(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
    with session_factory() as session:
        n = process_running_tasks(session)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert len(cards) == len(kps)  # 每知识点一张卡
    assert task.generated_card_count == len(cards)
    assert all(c.source == "GENERATED" for c in cards)


def test_executor_no_duplicate_generation_items(session_factory: Callable[[], Session]) -> None:
    """generation_item_id 部分唯一索引防重：二次执行不重复入库。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
    with session_factory() as session:
        process_running_tasks(session)
        session.commit()
    # 已完成任务不再处理
    with session_factory() as session:
        n = process_running_tasks(session)
        session.commit()
    assert n == 0
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        item_ids = [c.generation_item_id for c in cards]
    assert len(item_ids) == len(set(item_ids))  # 无重复
