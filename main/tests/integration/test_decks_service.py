"""services.decks 集成测试：创建/列表/详情/删除/进度/删除保护（真实 SQLite）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Device
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import (
    create_deck,
    deck_progress,
    delete_deck,
    get_deck,
    list_decks,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'decks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_device(session: Session, device_id: str) -> None:
    """devices 行先落库：HTTP 流由 F1 设备中间件自动建立，service 测试需显式建立（FK 强制）。"""
    session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
    session.flush()


def test_decks_create_assigns_defaults(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        _ensure_device(session, device)
        deck = create_deck(session, device_id=device, name="学习", now="2026-08-11T00:00:00.000Z")
        session.commit()
        deck_id = deck.deck_id
    assert deck.name == "学习"
    assert deck.source == "MANUAL"
    assert deck.version == "2026-08-11T00:00:00.000Z"
    assert deck.created_at == "2026-08-11T00:00:00.000Z"
    # 进度派生：空牌组
    with session_factory() as session:
        progress = deck_progress(
            session, device_id=device, deck_id=deck_id, now="2026-08-11T00:00:00.000Z"
        )
    assert progress == {
        "card_count": 0,
        "due_count": 0,
        "mastered_card_count": 0,
        "review_count": 0,
        "mastery_ratio": 0.0,
    }


def test_decks_list_isolated_per_device(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        _ensure_device(session, device_a)
        _ensure_device(session, device_b)
        create_deck(session, device_id=device_a, name="A", now="2026-08-11T00:00:00.000Z")
        create_deck(session, device_id=device_b, name="B", now="2026-08-11T00:00:00.000Z")
        session.commit()
    with session_factory() as session:
        decks_a = list_decks(session, device_id=device_a, now="2026-08-11T00:00:00.000Z")
        decks_b = list_decks(session, device_id=device_b, now="2026-08-11T00:00:00.000Z")
    assert [d["name"] for d in decks_a] == ["A"]
    assert [d["name"] for d in decks_b] == ["B"]


def test_decks_get_other_device_returns_404(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        _ensure_device(session, device_a)
        _ensure_device(session, device_b)
        deck = create_deck(session, device_id=device_a, name="A", now="2026-08-11T00:00:00.000Z")
        session.commit()
        deck_id = deck.deck_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_deck(session, device_id=device_b, deck_id=deck_id, now="2026-08-11T00:00:00.000Z")
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_decks_delete_removes_cascade_and_sets_null(session_factory: Callable[[], Session]) -> None:
    """删除：cards 级联清理（含 review_states），tasks.deck_id SET NULL，重复删除返回 404。"""
    from infra.db.models import Card, ReviewState, Task
    from infra.db.models import Deck as DeckModel

    device = _uuid()
    with session_factory() as session:
        _ensure_device(session, device)
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        # 插入一张卡 + 初始 review_state + 一个终态任务引用
        card = Card(
            card_id=_uuid(),
            deck_id=deck.deck_id,
            device_id=device,
            source="MANUAL",
            position=1,
            front="f",
            back="b",
            card_type="QUESTION",
            version="v1",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(card)
        session.flush()
        session.add(
            ReviewState(
                review_state_id=_uuid(),
                card_id=card.card_id,
                state="NEW",
                stability=0.0,
                difficulty=1.0,
                due="2026-08-11T00:00:00.000Z",
                reps=0,
                lapses=0,
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        task = Task(
            task_id=_uuid(),
            device_id=device,
            status="COMPLETED",
            selected_chapters="[]",
            generation_config="{}",
            deck_id=deck.deck_id,
            generated_card_count=0,
            resumable=0,
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(task)
        session.commit()
        deck_id, card_id, task_id = deck.deck_id, card.card_id, task.task_id
    # 删除
    with session_factory() as session:
        delete_deck(session, device_id=device, deck_id=deck_id)
        session.commit()
    with session_factory() as session:
        assert session.get(DeckModel, deck_id) is None
        assert session.get(Card, card_id) is None
        # 级联：ReviewState 主键是 review_state_id（card_id 为 UNIQUE 列），须按 card_id 查询
        assert session.scalar(select(ReviewState).where(ReviewState.card_id == card_id)) is None
        task_row = session.get(Task, task_id)
        assert task_row is not None
        assert task_row.deck_id is None  # SET NULL
        # 重复删除：牌组已不存在 → DECK_NOT_FOUND（API 级"重复提交安全返回"由 Task 4 幂等键层保证）
        with pytest.raises(AppError) as excinfo:
            delete_deck(session, device_id=device, deck_id=deck_id)
        assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_decks_delete_blocked_by_non_terminal_task(session_factory: Callable[[], Session]) -> None:
    from infra.db.models import Task

    device = _uuid()
    with session_factory() as session:
        _ensure_device(session, device)
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        session.add(
            Task(
                task_id=_uuid(),
                device_id=device,
                status="RUNNING",
                selected_chapters="[]",
                generation_config="{}",
                deck_id=deck.deck_id,
                generated_card_count=0,
                resumable=0,
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
        deck_id = deck.deck_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        delete_deck(session, device_id=device, deck_id=deck_id)
    assert excinfo.value.code is ErrorCode.TASK_IN_PROGRESS
