"""services.study：今日学习计划（structure-contract 3.20/6.6；openapi /study/today，V2.5 新增）。

主计划（4.5）：当前项目全部已学习（state != NEW）且到期（due <= now）的可见卡 →
按遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序取到每日目标 → 仍有余额时从项目
学习范围（selected_new_card_chapter_ids + include_unassigned）中的 NEW 卡按选定章节
顺序、position、card_id 补足。学习日期与今日去重完成数按账号 IANA 学习时区分桶
（UTC reviewed_at 折算，不改写事件；helpers 在 services.preferences.service）。

实现裁决（Task 10，报告同步）：
- 计划容量 = 每日目标（固定）：已评级卡 due 自动推到未来、NEW 卡评级后离开 NEW 集，
  重取计划时自然不再出现——返回 cards 长度即主计划剩余数（"继续复习"的积压卡由下次
  到期的队列继续供给）；不按当日已完成数收缩容量（完成数超目标仍可继续复习积压）。
- due_count = 已学习且到期的可见卡数（NEW 卡不是"待复习"，不计入）。
- 新卡章节顺序 = 学习设置保存的选定章节数组顺序（FR-09 选定章节顺序）；
  未归属分组（include_unassigned=true）排在所有选定章节之后。

事务语义：本模块不 commit/rollback，调用方控制；get-or-create（偏好默认行）为物化写，
由 handler 提交（与 GET /projects/{id}/study-settings 同款）。
"""

import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import Card, Deck, ProjectStudySettings, ReviewEvent, ReviewState
from services.cards.service import card_view
from services.preferences.service import day_bounds_utc, get_preferences, learning_date
from services.projects.service import _owned_project, project_view
from services.review.service import review_state_view
from services.scheduling.scheduler import forgetting_risk

# database-design §0 时间戳字符串格式（恒 3 位毫秒 Z）
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def _overdue_seconds(now_dt: datetime, due: str) -> float:
    """逾期时长（秒）：now - due（due <= now 过滤后非负）。"""
    return (now_dt - _parse_utc(due)).total_seconds()


def _due_plan_cards(
    session: Session, *, deck_ids: list[str], now: str, now_dt: datetime, goal: int
) -> tuple[list[tuple[Card, ReviewState]], int]:
    """已学习且到期可见卡（state != NEW、due <= now）→ 排序 → 取到每日目标。

    返回 (计划内到期卡, 到期总数)。排序键：(-遗忘风险, -逾期时长, card_id)。
    """
    rows = list(
        session.execute(
            select(Card, ReviewState)
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.deck_id.in_(deck_ids),
                ReviewState.state != "NEW",
                ReviewState.due <= now,
                text(VISIBLE_PREDICATE_SQL),
            )
        ).all()
    )

    def risk_of(card: Card, rs: ReviewState) -> float:
        return forgetting_risk(rs.stability, rs.last_review, now_dt)

    rows.sort(
        key=lambda row: (
            -risk_of(row[0], row[1]),
            -_overdue_seconds(now_dt, row[1].due),
            row[0].card_id,
        )
    )
    return [(row[0], row[1]) for row in rows[:goal]], len(rows)


def _new_fill_cards(
    session: Session, *, deck_ids: list[str], project_id: str, remaining: int
) -> list[tuple[Card, ReviewState]]:
    """范围内 NEW 可见卡按选定章节顺序、position、card_id 补足（3.20/FR-02/FR-09）。

    章节范围 = ProjectStudySettings.selected_chapter_ids（数组顺序即选定顺序）；
    include_unassigned 决定 chapter_id=null 的"未归属"新卡是否进入（排在最后）。
    范围外章节与未归属（未开启时）的新卡一律不进入计划。
    """
    if remaining <= 0:
        return []
    rows = list(
        session.execute(
            select(Card, ReviewState)
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.deck_id.in_(deck_ids),
                ReviewState.state == "NEW",
                text(VISIBLE_PREDICATE_SQL),
            )
        ).all()
    )
    settings = session.get(ProjectStudySettings, project_id)
    if settings is None:
        return []
    try:
        selected = cast(list[str], json.loads(settings.selected_chapter_ids))
    except (ValueError, TypeError):
        selected = []
    include_unassigned = bool(settings.include_unassigned)

    def chapter_index(card: Card) -> int:
        """选定章节 → 数组下标；未归属 → len(selected)（排在最后）；范围外 → -1（排除）。"""
        if card.chapter_id is None:
            return len(selected) if include_unassigned else -1
        try:
            return selected.index(card.chapter_id)
        except ValueError:
            return -1

    scoped = [(card, rs) for card, rs in rows if chapter_index(card) >= 0]
    scoped.sort(key=lambda row: (chapter_index(row[0]), row[0].position, row[0].card_id))
    return scoped[:remaining]


def _plan_card_view(card: Card, rs: ReviewState, now_dt: datetime) -> dict[str, object]:
    """TodayPlanCard：卡平铺 + review_state + forgetting_risk（无法计算时置 0）。"""
    return {
        **card_view(card),
        "review_state": review_state_view(rs),
        "forgetting_risk": forgetting_risk(rs.stability, rs.last_review, now_dt),
    }


def today_study_plan(session: Session, *, user_id: str, now: str) -> dict[str, object]:
    """当前项目今日计划（3.20）：无当前项目时 current_project=null（空态）。"""
    now_dt = _parse_utc(now)
    prefs = get_preferences(session, user_id=user_id, now=now_dt)
    timezone: str = prefs["learning_timezone"]
    study_date = learning_date(now, timezone)
    day_start, day_end = day_bounds_utc(now, timezone)
    completed = (
        session.scalar(
            select(func.count(func.distinct(ReviewEvent.card_id))).where(
                ReviewEvent.user_id == user_id,
                ReviewEvent.reviewed_at >= day_start,
                ReviewEvent.reviewed_at < day_end,
            )
        )
        or 0
    )
    daily_goal: int = prefs["daily_learning_goal"]
    project_id: str | None = prefs["current_project_id"]
    if project_id is None:
        return {
            "timezone": timezone,
            "study_date": study_date,
            "current_project": None,
            "daily_goal": daily_goal,
            "today_completed_count": completed,
            "due_count": 0,
            "main_plan_remaining": 0,
            "backlog_count": 0,
            "cards": [],
        }
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    deck_ids = list(
        session.scalars(
            select(Deck.deck_id).where(Deck.project_id == project_id, Deck.user_id == user_id)
        ).all()
    )
    due_cards, due_count = _due_plan_cards(
        session, deck_ids=deck_ids, now=now, now_dt=now_dt, goal=daily_goal
    )
    new_cards = _new_fill_cards(
        session,
        deck_ids=deck_ids,
        project_id=project_id,
        remaining=daily_goal - len(due_cards),
    )
    cards = [_plan_card_view(card, rs, now_dt) for card, rs in [*due_cards, *new_cards]]
    return {
        "timezone": timezone,
        "study_date": study_date,
        "current_project": project_view(session, project),
        "daily_goal": daily_goal,
        "today_completed_count": completed,
        "due_count": due_count,
        "main_plan_remaining": len(cards),
        "backlog_count": max(0, due_count - daily_goal),
        "cards": cards,
    }
