"""services.stats 看板聚合集成测试（真实 SQLite + zoneinfo 分桶）。

与 brief 草稿的修正（校准结论登记 task-4-report）：
- FK 强制（engine 级 PRAGMA foreign_keys=ON）：_seed_events 前置 User/Deck/Card 行
  （carry-forward：test_review_service 已实证违约）；每事件一张卡（card_id FK → cards）；
- 周日事件种子校准：brief 原 "2026-08-16T23:00:00.000Z" 经 Asia/Shanghai（UTC+8）换算为
  8/17（周一）07:00，落在下周 → 校准为 "2026-08-16T15:00:00.000Z"（上海 8/16 23:00 周日），
  保持"周日事件"分桶意图；
- 周号/周界实测：2026-08-11 为周二（weekday=1）、ISO 周 33；上海周界
  start=2026-08-09T16:00:00.000Z（周一 00:00 +08）、end=2026-08-16T16:00:00.000Z；
- retention 口径：种子每事件新卡 → 各自首次 → 非首次分母 0 → None；另补同卡两事件用例
  （首 GOOD + 次 AGAIN → 非首次 1 个（AGAIN）→ retention = 0.0）。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Card, ReviewEvent, ReviewState, User
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.stats.service import dashboard


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'stats.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)  # 实测周二（weekday=1）、ISO 周 33


def _seed_user(session: Session, *, user_id: str) -> None:
    """users 行前置（FK 强制，HTTP 流由注册端点建立）。"""
    if session.scalar(select(User).where(User.user_id == user_id)) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                password_hash="x",
                created_at="2026-01-01T00:00:00.000Z",
                updated_at="2026-01-01T00:00:00.000Z",
            )
        )
        session.flush()


def _seed_events(
    session: Session, *, user_id: str, events: list[tuple[str, str, str]], same_card: bool = False
) -> None:
    """事件种子：(client_event_id, rating, reviewed_at)；FK 前置 user/deck/card。

    same_card=True 时全部事件共用一张卡（retention 非首次用例）。
    """
    _seed_user(session, user_id=user_id)
    deck = create_deck(session, user_id=user_id, name="D", now="2026-01-01T00:00:00.000Z")
    session.flush()
    card_ids: list[str] = []
    for i, (client_event, rating, reviewed_at) in enumerate(events):
        cid = card_ids[0] if same_card and card_ids else _uuid()
        if cid not in card_ids:
            session.add(
                Card(
                    card_id=cid,
                    deck_id=deck.deck_id,
                    user_id=user_id,
                    source="MANUAL",
                    position=i + 1,
                    front="f",
                    back="b",
                    card_type="QUESTION",
                    version="v1",
                    created_at=reviewed_at,
                    updated_at=reviewed_at,
                )
            )
            session.flush()
        card_ids.append(cid)
        session.add(
            ReviewEvent(
                review_event_id=_uuid(),
                user_id=user_id,
                card_id=cid,
                client_event_id=client_event,
                rating=rating,
                reviewed_at=reviewed_at,
                device_timezone="Asia/Shanghai",
                created_at=reviewed_at,
            )
        )


def test_stats_dashboard_empty_has_data_false(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["has_data"] is False
    assert result["weekly_total"] == 0
    assert result["weekly_activity"] == [0] * 7
    assert result["week_change_rate"] is None
    assert result["weekly_goal"] is None
    assert result["weekly_goal_progress"] is None
    assert result["recall_accuracy"] is None
    assert result["first_answer_accuracy"] is None
    assert result["retention_rate"] is None
    assert result["streak_days"] == 0
    assert result["mastered_card_count"] == 0
    period = result["period"]
    assert isinstance(period, dict)
    assert period["week_ordinal"] == 33


def test_stats_dashboard_weekly_bucketing_monday(session_factory: Callable[[], Session]) -> None:
    """周一分桶：2026-08-10（周一）~2026-08-16（周日）为当前周（Asia/Shanghai，UTC+8）。"""
    user = _uuid()
    # 周一事件 + 周日事件 + 上周事件 + 下周一事件
    monday = "2026-08-10T01:00:00.000Z"  # 上海周一 09:00
    sunday = (
        "2026-08-16T15:00:00.000Z"  # 上海周日 23:00（brief 原 23:00Z 换算为上海周一 07:00 → 校准）
    )
    last_week = "2026-08-03T10:00:00.000Z"  # 上海 8/03（周一）18:00 → 上周
    next_monday = "2026-08-17T00:00:00.000Z"  # 上海周一 08:00 → 下周
    with session_factory() as session:
        _seed_events(
            session,
            user_id=user,
            events=[
                ("e1", "GOOD", monday),
                ("e2", "AGAIN", sunday),
                ("e3", "GOOD", last_week),
                ("e4", "GOOD", next_monday),
            ],
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=10, now=_now()
        )
    assert result["has_data"] is True
    assert result["weekly_total"] == 2  # 周一+周日
    assert result["weekly_activity"] == [1, 0, 0, 0, 0, 0, 1]  # 周一~周日
    assert result["week_change_rate"] == 1.0  # (2-1)/1
    assert result["weekly_goal"] == 10
    assert result["weekly_goal_progress"] == 0.2
    assert result["recall_accuracy"] == 0.5  # 1 GOOD / 2
    assert result["retention_rate"] is None  # 每事件各自卡的首次 → 非首次分母 0
    period = result["period"]
    assert isinstance(period, dict)
    assert period["start"] == "2026-08-09T16:00:00.000Z"  # 上海周一 00:00
    assert period["end"] == "2026-08-16T16:00:00.000Z"  # 下周一 00:00
    assert period["week_ordinal"] == 33  # ISO 周号


def test_stats_dashboard_week_change_null_when_last_week_zero(
    session_factory: Callable[[], Session],
) -> None:
    user = _uuid()
    with session_factory() as session:
        _seed_events(session, user_id=user, events=[("e1", "GOOD", "2026-08-10T01:00:00.000Z")])
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["week_change_rate"] is None  # 上周 0 → null


def test_stats_dashboard_first_answer_accuracy_historical(
    session_factory: Callable[[], Session],
) -> None:
    """first_answer_accuracy：每卡历史首个事件为 GOOD 的比例（跨周期累计）。"""
    user = _uuid()
    with session_factory() as session:
        _seed_events(
            session,
            user_id=user,
            events=[
                ("e1", "GOOD", "2026-08-10T01:00:00.000Z"),
                ("e2", "AGAIN", "2026-08-11T01:00:00.000Z"),
            ],
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["first_answer_accuracy"] == 0.5


def test_stats_dashboard_retention_non_first_same_card(
    session_factory: Callable[[], Session],
) -> None:
    """retention_rate：周内非首次事件（该卡此前已有更早事件）GOOD 占比；首 GOOD+次 AGAIN → 0.0。"""
    user = _uuid()
    with session_factory() as session:
        _seed_events(
            session,
            user_id=user,
            events=[
                ("e1", "GOOD", "2026-08-10T01:00:00.000Z"),
                ("e2", "AGAIN", "2026-08-11T01:00:00.000Z"),
            ],
            same_card=True,
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["retention_rate"] == 0.0  # 非首次 1 个（e2 AGAIN）
    assert result["first_answer_accuracy"] == 1.0  # 该卡首个事件 e1 GOOD
    assert result["recall_accuracy"] == 0.5


def test_stats_dashboard_streak_days(session_factory: Callable[[], Session]) -> None:
    """连续学习天数：截至本地当天（Asia/Shanghai）连续有事件的自然日数。"""
    user = _uuid()
    # 今天 8/11、昨天 8/10、前天 8/09 有事件；8/08 无
    with session_factory() as session:
        _seed_events(
            session,
            user_id=user,
            events=[
                ("e1", "GOOD", "2026-08-09T01:00:00.000Z"),
                ("e2", "GOOD", "2026-08-10T01:00:00.000Z"),
                ("e3", "GOOD", "2026-08-11T01:00:00.000Z"),
            ],
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["streak_days"] == 3


def test_stats_dashboard_mastered_count(session_factory: Callable[[], Session]) -> None:
    """已掌握卡片：C-03（REVIEW 且 stability>=21）去重计数；无周事件但已掌握>0 → has_data True。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        deck = create_deck(session, user_id=user, name="D", now="2026-01-01T00:00:00.000Z")
        session.flush()
        for i, (state, stability) in enumerate([("REVIEW", 25.0), ("REVIEW", 10.0), ("NEW", 30.0)]):
            card = Card(
                card_id=_uuid(),
                deck_id=deck.deck_id,
                user_id=user,
                source="MANUAL",
                position=i + 1,
                front="f",
                back="b",
                card_type="QUESTION",
                version="v1",
                created_at="2026-01-01T00:00:00.000Z",
                updated_at="2026-01-01T00:00:00.000Z",
            )
            session.add(card)
            session.flush()
            session.add(
                ReviewState(
                    review_state_id=_uuid(),
                    card_id=card.card_id,
                    state=state,
                    stability=stability,
                    difficulty=5.0,
                    due="2026-01-01T00:00:00.000Z",
                    reps=1,
                    lapses=0,
                    updated_at="2026-01-01T00:00:00.000Z",
                )
            )
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session, user_id=user, timezone="Asia/Shanghai", weekly_goal=None, now=_now()
        )
    assert result["mastered_card_count"] == 1
    assert result["has_data"] is True  # mastered>0 即使周内无事件


def test_stats_dashboard_invalid_timezone_rejected(session_factory: Callable[[], Session]) -> None:
    """非法 IANA 时区 → AppError(VALIDATION_ERROR)（400）。"""
    user = _uuid()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        dashboard(session, user_id=user, timezone="Not/AZone", weekly_goal=None, now=_now())
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
