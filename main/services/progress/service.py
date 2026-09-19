"""卡组、项目和周计划的真实学习进度聚合。

这里不保存可漂移的计数器：ReviewEvent 是事实，ReviewState 是当前快照，所有展示字段
均由可见卡片即时聚合得到。项目级周统计端点已随 V25-D-39 账号级计划退役
（计划不再归属项目，无项目周目标语义）。
"""

from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import (
    Card,
    Deck,
    ReviewEvent,
    ReviewState,
)
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
                select(Deck.deck_id).where(Deck.user_id == user_id, Deck.project_id == project_id)
            ).all()
        )
    else:
        deck = session.scalar(select(Deck).where(Deck.deck_id == deck_id, Deck.user_id == user_id))
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
    # 聚合下推：只取 count 与 max，不把全部事件日期串拉进内存。
    event_count, last_studied = session.execute(
        select(func.count(ReviewEvent.review_event_id), func.max(ReviewEvent.reviewed_at))
        .join(Card, (Card.card_id == ReviewEvent.card_id) & (Card.user_id == user_id))
        .where(
            ReviewEvent.user_id == user_id,
            Card.user_id == user_id,
            Card.deck_id.in_(deck_ids) if deck_ids else text("0 = 1"),
            visible,
        )
    ).one()
    return {
        "card_count": len(rows),
        **counts,
        "due_count": due_count,
        "review_event_count": int(event_count),
        "last_studied_at": last_studied,
    }


def project_progress(
    session: Session, *, user_id: str, project_id: str, now: str
) -> dict[str, object]:
    return progress_summary(session, user_id=user_id, project_id=project_id, now=now)
