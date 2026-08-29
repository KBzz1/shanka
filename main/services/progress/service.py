"""卡组、项目和周计划的真实学习进度聚合。

这里不保存可漂移的计数器：ReviewEvent 是事实，ReviewState 是当前快照，所有展示字段
均由可见卡片即时聚合得到。
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import (
    Card,
    Deck,
    ProjectStudyDeck,
    ProjectStudySettings,
    ReviewEvent,
    ReviewState,
)
from infra.db.session import format_utc
from services.preferences.service import get_preferences, learning_date
from services.projects.service import _owned_project


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _classify(state: str | None, stability: float | None) -> str:
    if state is None or state == "NEW":
        return "not_started_count"
    if state == "LEARNING":
        return "learning_count"
    if state == "RELEARNING":
        return "relearning_count"
    if state == "REVIEW" and float(stability or 0) >= 21:
        return "mastered_count"
    return "consolidating_count"


def progress_summary(
    session: Session,
    *,
    user_id: str,
    now: str,
    deck_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, object]:
    """按卡组或项目聚合真实阶段进度。"""
    if (deck_id is None) == (project_id is None):
        raise ValueError("exactly one of deck_id/project_id is required")
    if project_id is not None:
        _owned_project(session, user_id=user_id, project_id=project_id)
        deck_ids = list(
            session.scalars(
                select(Deck.deck_id).where(
                    Deck.user_id == user_id, Deck.project_id == project_id
                )
            ).all()
        )
    else:
        deck = session.scalar(
            select(Deck).where(Deck.deck_id == deck_id, Deck.user_id == user_id)
        )
        if deck is None:
            raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在")
        deck_ids = [deck.deck_id]
    visible = text(VISIBLE_PREDICATE_SQL)
    rows = list(
        session.execute(
            select(Card.card_id, ReviewState.state, ReviewState.stability, ReviewState.due)
            .select_from(Card)
            .outerjoin(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id.in_(deck_ids) if deck_ids else text("0 = 1"),
                visible,
            )
        ).all()
    )
    counts = {
        "not_started_count": 0,
        "learning_count": 0,
        "relearning_count": 0,
        "consolidating_count": 0,
        "mastered_count": 0,
    }
    due_count = 0
    for _card_id, state, stability, due in rows:
        counts[_classify(state, stability)] += 1
        # A legacy/imported card may be missing its one-to-one ReviewState row.  It still counts
        # toward the visible card total, but without a due timestamp it cannot be considered
        # currently due.
        if state is not None and state != "NEW" and due is not None and due <= now:
            due_count += 1
    events_query = (
        select(ReviewEvent.reviewed_at)
        .join(Card, Card.card_id == ReviewEvent.card_id)
        .where(
            ReviewEvent.user_id == user_id,
            Card.user_id == user_id,
            Card.deck_id.in_(deck_ids) if deck_ids else text("0 = 1"),
            visible,
        )
    )
    event_dates = list(session.scalars(events_query).all())
    return {
        "card_count": len(rows),
        **counts,
        "due_count": due_count,
        "review_event_count": len(event_dates),
        "last_studied_at": max(event_dates) if event_dates else None,
    }


def project_progress(
    session: Session, *, user_id: str, project_id: str, now: str
) -> dict[str, object]:
    return progress_summary(session, user_id=user_id, project_id=project_id, now=now)


def project_weekly_stats(
    session: Session, *, user_id: str, project_id: str, now: datetime
) -> dict[str, object]:
    """当前项目所选计划卡组的周活动和双目标统计。"""
    _owned_project(session, user_id=user_id, project_id=project_id)
    prefs = get_preferences(session, user_id=user_id, now=now)
    timezone = str(prefs["learning_timezone"])
    tz = ZoneInfo(timezone)
    local_date = now.astimezone(tz).date()
    monday = local_date - timedelta(days=local_date.weekday())
    start = datetime.combine(monday, datetime.min.time(), tzinfo=tz)
    end = start + timedelta(days=7)
    settings = session.get(ProjectStudySettings, project_id)
    selected = list(
        session.scalars(
            select(ProjectStudyDeck.deck_id).where(ProjectStudyDeck.project_id == project_id)
        ).all()
    )
    # New projects have an explicit empty settings row, which means the plan is intentionally
    # unconfigured: do not silently turn every project deck into a weekly target.  Only a truly
    # pre-plan row (no settings record at all) uses the legacy all-project-decks fallback.
    if settings is None:
        selected = list(
            session.scalars(select(Deck.deck_id).where(Deck.project_id == project_id)).all()
        )
    visible = text(VISIBLE_PREDICATE_SQL)
    rows = list(
        session.execute(
            select(
                ReviewEvent.review_event_id,
                ReviewEvent.card_id,
                ReviewEvent.rating,
                ReviewEvent.reviewed_at,
            )
            .join(Card, Card.card_id == ReviewEvent.card_id)
            .where(
                ReviewEvent.user_id == user_id,
                Card.user_id == user_id,
                Card.deck_id.in_(selected) if selected else text("0 = 1"),
                visible,
            )
            .order_by(ReviewEvent.card_id, ReviewEvent.reviewed_at, ReviewEvent.created_at)
        ).all()
    )
    start_str, end_str = format_utc(start.astimezone(UTC)), format_utc(end.astimezone(UTC))
    week = [row for row in rows if start_str <= row.reviewed_at < end_str]
    weekly_activity = [0] * 7
    for row in week:
        weekly_activity[date.fromisoformat(learning_date(row.reviewed_at, timezone)).weekday()] += 1
    # Event ids disambiguate same-millisecond ratings; using reviewed_at alone could count two
    # events for one card as its first (new-card) completion.
    first_seen: dict[str, str] = {}
    for row in rows:
        first_seen.setdefault(row.card_id, row.review_event_id)
    new_done = {
        row.card_id
        for row in week
        if row.review_event_id == first_seen.get(row.card_id)
    }
    review_done = {
        row.card_id
        for row in week
        if row.review_event_id != first_seen.get(row.card_id)
        and row.card_id not in new_done
    }
    configured = bool(selected) and settings is not None
    new_goal = int(settings.daily_new_goal) * 7 if configured and settings else 0
    review_goal = int(settings.daily_review_goal) * 7 if configured and settings else 0
    weekly_goal = new_goal + review_goal
    completed = len({(learning_date(row.reviewed_at, timezone), row.card_id) for row in week})
    return {
        "project_id": project_id,
        "period_start": start_str,
        "period_end": end_str,
        "timezone": timezone,
        "weekly_activity": weekly_activity,
        "weekly_total": len(week),
        "weekly_completed_count": completed,
        "weekly_new_goal": new_goal,
        "weekly_review_goal": review_goal,
        "weekly_goal": weekly_goal,
        "weekly_goal_progress": min(completed / weekly_goal, 1.0) if weekly_goal else None,
        "new_completed_count": len(new_done),
        "review_completed_count": len(review_done),
        "updated_at": format_utc(now),
    }
