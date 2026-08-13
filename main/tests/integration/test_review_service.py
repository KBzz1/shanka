"""services.review 集成测试：到期队列/评级事务/client_event_id 兜底（真实 SQLite）。

与 brief 草稿的修正（校准说明）：
- result["state"] / rs.state 用大写（契约 3.10 枚举，裁决 1：fsrs State .name.upper() 落库）；
- fixture 补 users 行（FK 强制，carry-forward：test_cards_service 已实证违约）；
- 初始难度断言 1.0（ORM CHECK 1~10 的 V1 占位值）；
- fix round 1：Learning 卡重建 step 由 due-last_review 间隔推导（裁决 2），
  覆盖二次 GOOD +1d / 三次 GOOD 毕业 / RELEARNING 重建 GOOD → REVIEW。
"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, ReviewEvent, ReviewState, User
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
def user() -> str:
    return _uuid()


@pytest.fixture
def deck_and_card(session_factory: Callable[[], Session], user: str) -> tuple[str, str]:
    """牌组+卡片归属 user；users 行前置（FK 强制，HTTP 流由注册端点建立）。"""
    with session_factory() as session:
        session.add(
            User(
                user_id=user,
                username=f"u-{user[:8]}",
                password_hash="x",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        deck = create_deck(session, user_id=user, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        card = create_card(
            session,
            user_id=user,
            deck_id=deck.deck_id,
            front="f",
            back="b",
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        return deck.deck_id, card.card_id


def test_review_queue_returns_due_cards_sorted(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    deck_id, card_id = deck_and_card
    with session_factory() as session:
        items = review_queue(session, user_id=user, deck_id=deck_id, now="2026-08-11T01:00:00.000Z")
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
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    deck_id, _ = deck_and_card
    with session_factory() as session:
        items = review_queue(session, user_id=user, deck_id=deck_id, now="2026-08-10T00:00:00.000Z")
    assert items == []


def test_review_queue_cross_user_404(
    session_factory: Callable[[], Session], deck_and_card: tuple[str, str]
) -> None:
    deck_id, _ = deck_and_card
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        review_queue(session, user_id=_uuid(), deck_id=deck_id, now="2026-08-11T01:00:00.000Z")
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_review_queue_sorts_by_due_then_position(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """排序（5.15/6.6，I-2）：due 不同先到期者在前；同 due 按 position 升序。"""
    deck_id, _ = deck_and_card  # 已含 00:00 创建的卡（pos 1、due 00:00）
    with session_factory() as session:
        create_card(
            session,
            user_id=user,
            deck_id=deck_id,
            front="f2",
            back="b2",
            now="2026-08-11T00:00:00.000Z",
        )  # pos 2、due 00:00 同前卡
        create_card(
            session,
            user_id=user,
            deck_id=deck_id,
            front="f3",
            back="b3",
            now="2026-08-11T00:30:00.000Z",
        )  # pos 3、due 00:30
        session.commit()
    with session_factory() as session:
        items = review_queue(session, user_id=user, deck_id=deck_id, now="2026-08-11T01:00:00.000Z")
    assert len(items) == 3
    ordered: list[tuple[str, int]] = []
    for item in items:
        rs = item["review_state"]
        assert isinstance(rs, dict)
        ordered.append((str(rs["due"]), cast(int, item["position"])))
    assert ordered == [
        ("2026-08-11T00:00:00.000Z", 1),
        ("2026-08-11T00:00:00.000Z", 2),
        ("2026-08-11T00:30:00.000Z", 3),
    ]


def test_submit_review_updates_state_and_creates_event(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """新卡首次 GOOD → Learning、due=now+10m（5.2 表第 1 行，R-13 3 步配置）；事件同事务落库。"""
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        result = submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert result["state"] == "LEARNING"
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
        assert rs.state == "LEARNING"
        assert rs.due == "2026-08-11T01:10:00.000Z"


def test_submit_review_same_client_event_replays(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        first = submit_review(
            session,
            user_id=user,
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
            user_id=user,
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
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    client_event = _uuid()
    with session_factory() as session:
        submit_review(
            session,
            user_id=user,
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
            user_id=user,
            card_id=card_id,
            rating="AGAIN",
            client_event_id=client_event,
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.REVIEW_EVENT_CONFLICT


def test_submit_review_cross_user_404(
    session_factory: Callable[[], Session], deck_and_card: tuple[str, str]
) -> None:
    _, card_id = deck_and_card
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        submit_review(
            session,
            user_id=_uuid(),
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND


def test_submit_review_rollback_on_failure(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """评级失败（非法 rating）→ 无事件无状态变更（同事务回滚）。"""
    _, card_id = deck_and_card
    with session_factory() as session:
        with pytest.raises(AppError):
            submit_review(
                session,
                user_id=user,
                card_id=card_id,
                rating="MAYBE",
                client_event_id=_uuid(),
                device_timezone="Asia/Shanghai",
                now="2026-08-11T01:00:00.000Z",
            )
        session.rollback()
    with session_factory() as session:
        assert len(session.scalars(select(ReviewEvent)).all()) == 0
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.reps == 0  # 状态快照亦未变更（同事务回滚，M-4）


def test_submit_review_learning_second_good_plus_1d(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """Learning 卡重建（step 由 due-last_review 推导，裁决 2）：二次 GOOD → +1d（5.2 表第 2 行）。"""
    _, card_id = deck_and_card
    with session_factory() as session:
        first = submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert first["state"] == "LEARNING"
    assert first["due"] == "2026-08-11T01:10:00.000Z"  # 首 GOOD → +10m（step 1，实证）
    with session_factory() as session:
        second = submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:10:00.000Z",
        )
        session.commit()
    assert second["state"] == "LEARNING"
    assert second["due"] == "2026-08-12T01:10:00.000Z"  # 重建 step=1 → 二次 GOOD +1d
    assert second["reps"] == 2


def test_submit_review_learning_graduates_and_relearning_rebuild(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """Learning 三次 GOOD 毕业 REVIEW（5.2 表第 3 行）；REVIEW+AGAIN → RELEARNING(+10m)；
    RELEARNING 重建（step=0）GOOD → REVIEW。"""
    _, card_id = deck_and_card
    steps = [
        ("GOOD", "2026-08-11T01:00:00.000Z"),
        ("GOOD", "2026-08-11T01:10:00.000Z"),
        ("GOOD", "2026-08-12T01:10:00.000Z"),
        ("AGAIN", "2026-08-19T01:10:00.000Z"),
        ("GOOD", "2026-08-19T01:20:00.000Z"),
    ]
    results: list[dict[str, object]] = []
    with session_factory() as session:
        for rating, now in steps:
            result = submit_review(
                session,
                user_id=user,
                card_id=card_id,
                rating=rating,
                client_event_id=_uuid(),
                device_timezone="Asia/Shanghai",
                now=now,
            )
            results.append(result)
        session.commit()
    assert results[0]["state"] == "LEARNING"  # +10m（首 GOOD）
    assert results[1]["state"] == "LEARNING"  # +1d（二次 GOOD，step 推导）
    assert results[2]["state"] == "REVIEW"  # 三次 GOOD 毕业（5.2 表第 3 行）
    assert results[3]["state"] == "RELEARNING"  # REVIEW+AGAIN
    assert results[3]["due"] == "2026-08-19T01:20:00.000Z"  # +10m（relearning_steps[0]）
    assert results[4]["state"] == "REVIEW"  # RELEARNING 重建 GOOD → Review
    assert str(results[4]["due"]) > "2026-08-19T01:20:00.000Z"


def test_submit_review_learning_againthen_good_plus_10m(
    session_factory: Callable[[], Session], user: str, deck_and_card: tuple[str, str]
) -> None:
    """AGAIN 后 Learning 卡（last_rating=AGAIN、间隔 10m）重建 GOOD → +10m（I-1 消歧，不跳巩固步）。"""
    _, card_id = deck_and_card
    with session_factory() as session:
        submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        again = submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="AGAIN",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:10:00.000Z",
        )
        session.commit()
    assert again["state"] == "LEARNING"
    assert again["due"] == "2026-08-11T01:20:00.000Z"  # AGAIN → step 0、+10m
    with session_factory() as session:
        third = submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now="2026-08-11T01:20:00.000Z",
        )
        session.commit()
    assert third["state"] == "LEARNING"
    assert (
        third["due"] == "2026-08-11T01:30:00.000Z"
    )  # last_rating=AGAIN → step 0 → GOOD +10m（非 +1d）
    assert third["reps"] == 3
    assert third["lapses"] == 1
