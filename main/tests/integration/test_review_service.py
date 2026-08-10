"""services.review 集成测试：到期队列/评级事务/client_event_id 兜底（真实 SQLite）。

与 brief 草稿的修正（校准说明）：
- result["state"] / rs.state 用 "Learning"（py-fsrs 4.1.2 State 枚举 .name，R-13 落地后落库口径）；
- fixture 补 devices 行（FK 强制，carry-forward：test_cards_service 已实证违约）；
- 初始难度断言 1.0（ORM CHECK 1~10 的 V1 占位值）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Device, ReviewEvent, ReviewState
from infra.db.session import create_db_engine, create_session_factory
from services.cards.service import create_card
from services.decks.service import create_deck
from services.review.service import review_queue, submit_review


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def device() -> str:
    return _uuid()


@pytest.fixture
def deck_and_card(session_factory: Callable[[], Session], device: str) -> tuple[str, str]:
    """牌组+卡片归属 device；devices 行前置（FK 强制，HTTP 流由 F1 设备中间件自动建立）。"""
    with session_factory() as session:
        session.add(Device(device_id=device, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        card = create_card(
            session,
            device_id=device,
            deck_id=deck.deck_id,
            front="f",
            back="b",
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        return deck.deck_id, card.card_id


def test_review_queue_returns_due_cards_sorted(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    deck_id, card_id = deck_and_card
    with session_factory() as session:
        items = review_queue(
            session, device_id=device, deck_id=deck_id, now="2026-08-11T01:00:00.000Z"
        )
    assert len(items) == 1
    assert items[0]["card_id"] == card_id
    rs_view = items[0]["review_state"]
    assert isinstance(rs_view, dict)
    assert rs_view["state"] == "NEW"
    assert rs_view["due"] == "2026-08-11T00:00:00.000Z"
    assert rs_view["stability"] == 0.0
    assert rs_view["difficulty"] == 1.0  # V1 初始占位值（ORM CHECK 1~10）
    assert rs_view["reps"] == 0


def test_review_queue_excludes_not_due(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    deck_id, _ = deck_and_card
    with session_factory() as session:
        items = review_queue(
            session, device_id=device, deck_id=deck_id, now="2026-08-10T00:00:00.000Z"
        )
    assert items == []


def test_review_queue_cross_device_404(
    session_factory: Callable[[], Session], deck_and_card: tuple[str, str]
) -> None:
    deck_id, _ = deck_and_card
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        review_queue(session, device_id=_uuid(), deck_id=deck_id, now="2026-08-11T01:00:00.000Z")
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_submit_review_updates_state_and_creates_event(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    """新卡首次 GOOD → Learning、due=now+10m（5.2 表第 1 行，R-13 3 步配置）；事件同事务落库。"""
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        result = submit_review(
            session,
            device_id=device,
            card_id=card_id,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert result["state"] == "Learning"
    assert result["due"] == "2026-08-11T01:10:00.000Z"
    assert result["reps"] == 1
    assert result["lapses"] == 0
    assert result["last_rating"] == "GOOD"
    with session_factory() as session:
        events = session.scalars(select(ReviewEvent)).all()
        assert len(events) == 1
        assert events[0].client_event_id == client_event
        assert events[0].rating == "GOOD"
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.reps == 1
        assert rs.state == "Learning"
        assert rs.due == "2026-08-11T01:10:00.000Z"


def test_submit_review_same_client_event_replays(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        first = submit_review(
            session,
            device_id=device,
            card_id=card_id,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        second = submit_review(
            session,
            device_id=device,
            card_id=card_id,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert second["reps"] == 1  # 不重复计数
    assert second["due"] == first["due"]  # 重放 = 读当前 review_state 视图
    with session_factory() as session:
        assert len(session.scalars(select(ReviewEvent)).all()) == 1


def test_submit_review_same_client_event_conflict(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        submit_review(
            session,
            device_id=device,
            card_id=card_id,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        submit_review(
            session,
            device_id=device,
            card_id=card_id,
            rating="AGAIN",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.REVIEW_EVENT_CONFLICT


def test_submit_review_cross_device_404(
    session_factory: Callable[[], Session], deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        submit_review(
            session,
            device_id=_uuid(),
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND


def test_submit_review_rollback_on_failure(
    session_factory: Callable[[], Session], device: str, deck_and_card: tuple[str, str]
) -> None:
    """评级失败（非法 rating）→ 无事件无状态变更（同事务回滚）。"""
    _, card_id = deck_and_card
    with session_factory() as session:
        with pytest.raises(AppError):
            submit_review(
                session,
                device_id=device,
                card_id=card_id,
                rating="MAYBE",
                client_event_id=_uuid(),
                device_timezone="Asia/Shanghai",
                now="2026-08-11T01:00:00.000Z",
            )
        session.rollback()
    with session_factory() as session:
        assert len(session.scalars(select(ReviewEvent)).all()) == 0
