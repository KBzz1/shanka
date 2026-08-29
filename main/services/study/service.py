"""services.study：今日学习计划（structure-contract 3.20/6.6；openapi /study/today，V2.5 新增）。

主计划（4.5）：当前计划所选卡组内已学习（state != NEW）且到期（due <= now）的可见卡 →
按遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序取巩固目标；再从同一批卡组的
NEW 卡按 deck_id、position、card_id 补足新学目标。学习日期与今日去重完成数按账号
IANA 学习时区分桶（UTC reviewed_at 折算，不改写事件；helpers 在 services.preferences.service）。
没有保存卡组范围的项目只返回未配置空态，不把项目内所有卡组偷偷加入今日计划。

计划容量裁决：巩固目标是软目标，核心队列先取最多目标张到期卡；完成核心队列后，
`/study/today/backlog` 可按同一排序继续读取积压。首页合计目标使用当天真实可供学习的
新卡与到期卡数量，避免到期卡不足时显示无法完成的固定上限。

事务语义：本模块不 commit/rollback，调用方控制；get-or-create（偏好默认行）为物化写，
由 handler 提交（与 GET /projects/{id}/study-settings 同款）。
"""

import json
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import delete, func, select, text
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
    UserPreferences,
)
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
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    now: str,
    now_dt: datetime,
    goal: int,
) -> tuple[list[tuple[Card, ReviewState]], int]:
    """已学习且到期可见卡（state != NEW、due <= now）→ 排序 → 取到每日目标。

    返回 (计划内到期卡, 到期总数)。排序键：(-遗忘风险, -逾期时长, card_id)。
    """
    rows = list(
        session.execute(
            select(Card, ReviewState)
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
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
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    project_id: str,
    remaining: int,
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
                Card.user_id == user_id,
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


def _new_deck_cards(
    session: Session, *, user_id: str, deck_ids: list[str], remaining: int
) -> list[tuple[Card, ReviewState]]:
    """新计划的新卡来源：只按选中的卡组和卡片位置稳定排序。"""
    if remaining <= 0 or not deck_ids:
        return []
    rows = list(
        session.execute(
            select(Card, ReviewState)
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id.in_(deck_ids),
                ReviewState.state == "NEW",
                text(VISIBLE_PREDICATE_SQL),
            )
        ).all()
    )
    rows.sort(key=lambda row: (row[0].deck_id, row[0].position, row[0].card_id))
    return [(row[0], row[1]) for row in rows[:remaining]]


def _plan_card_view(card: Card, rs: ReviewState, now_dt: datetime) -> dict[str, object]:
    """TodayPlanCard：卡平铺 + review_state + forgetting_risk（无法计算时置 0）。"""
    return {
        **card_view(card),
        "review_state": review_state_view(rs),
        "forgetting_risk": forgetting_risk(rs.stability, rs.last_review, now_dt),
    }


def _selected_plan_decks(session: Session, *, project_id: str) -> list[str]:
    return list(
        session.scalars(
            select(ProjectStudyDeck.deck_id)
            .where(ProjectStudyDeck.project_id == project_id)
            .order_by(ProjectStudyDeck.created_at, ProjectStudyDeck.deck_id)
        ).all()
    )


def _today_completed_by_kind(
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    day_start: str,
    day_end: str,
) -> tuple[int, int]:
    """返回今日已完成的新卡数、巩固卡数（同卡同日去重）。"""
    if not deck_ids:
        return 0, 0
    all_events = session.execute(
        select(ReviewEvent.review_event_id, ReviewEvent.card_id, ReviewEvent.reviewed_at)
        .join(Card, (Card.card_id == ReviewEvent.card_id) & (Card.user_id == user_id))
        .where(
            ReviewEvent.user_id == user_id,
            Card.deck_id.in_(deck_ids),
            text(VISIBLE_PREDICATE_SQL),
        )
        .order_by(ReviewEvent.card_id, ReviewEvent.reviewed_at, ReviewEvent.created_at)
    ).all()
    # Keep the event id rather than only reviewed_at: two ratings can legitimately share the
    # same millisecond, and timestamp equality alone would classify both as a card's first event.
    first_seen: dict[str, str] = {}
    for row in all_events:
        first_seen.setdefault(row.card_id, row.review_event_id)
    new_cards: set[str] = set()
    review_cards: set[str] = set()
    for row in all_events:
        if not (day_start <= row.reviewed_at < day_end):
            continue
        if row.review_event_id == first_seen.get(row.card_id):
            new_cards.add(row.card_id)
        else:
            review_cards.add(row.card_id)
    # 同一卡片当天首次评分与后续评分只归入新学，避免双重计数。
    review_cards.difference_update(new_cards)
    return len(new_cards), len(review_cards)


def _new_plan_today(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    project: Any,
    settings: ProjectStudySettings,
    timezone: str,
    study_date: str,
    day_start: str,
    day_end: str,
    now: str,
    now_dt: datetime,
) -> dict[str, object]:
    deck_ids = _selected_plan_decks(session, project_id=project_id)
    new_goal = int(settings.daily_new_goal)
    review_goal = int(settings.daily_review_goal)
    configured = bool(deck_ids) and new_goal + review_goal > 0
    new_completed, review_completed = _today_completed_by_kind(
        session,
        user_id=user_id,
        deck_ids=deck_ids,
        day_start=day_start,
        day_end=day_end,
    )
    all_due_cards, due_count = _due_plan_cards(
        session,
        user_id=user_id,
        deck_ids=deck_ids,
        now=now,
        now_dt=now_dt,
        goal=review_goal,
    )
    review_target = min(review_goal, due_count + review_completed)
    review_remaining = max(0, review_target - review_completed)
    due_cards = all_due_cards[:review_remaining]
    new_available = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id.in_(deck_ids) if deck_ids else text("0 = 1"),
                ReviewState.state == "NEW",
                text(VISIBLE_PREDICATE_SQL),
            )
        )
        or 0
    )
    new_target = min(new_goal, new_available + new_completed)
    new_remaining = max(0, new_target - new_completed)
    new_cards = _new_deck_cards(
        session, user_id=user_id, deck_ids=deck_ids, remaining=new_remaining
    )
    cards: list[dict[str, object]] = []
    for card, rs in due_cards:
        view = _plan_card_view(card, rs, now_dt)
        view["plan_kind"] = "DUE"
        cards.append(view)
    for card, rs in new_cards:
        view = _plan_card_view(card, rs, now_dt)
        view["plan_kind"] = "NEW"
        cards.append(view)
    return {
        "timezone": timezone,
        "study_date": study_date,
        "current_project": project_view(session, project),
        # The home card's denominator is the work that actually exists today, not the configured
        # ceiling.  A 40-card review goal with only 7 due cards must therefore show 17 when ten
        # new cards are available (and not an impossible 50).
        "daily_goal": new_target + review_target,
        "daily_new_goal": new_goal,
        "daily_review_goal": review_goal,
        "today_completed_count": new_completed + review_completed,
        "new_completed_count": new_completed,
        "review_completed_count": review_completed,
        "new_remaining_count": new_remaining,
        "review_remaining_count": review_remaining,
        "core_target_count": new_target + review_target,
        "due_count": due_count,
        "main_plan_remaining": len(cards),
        # Once the user has completed part of the soft goal, the remaining due cards move into
        # the optional backlog.  Keeping completed cards in this arithmetic makes the backlog
        # stable after the core queue is exhausted (e.g. 50 due / goal 40 remains 10, rather
        # than incorrectly dropping to zero after the first 40 cards are rated).
        "backlog_count": max(0, due_count - max(0, review_goal - review_completed)),
        "plan_configured": configured,
        "selected_deck_ids": deck_ids,
        "cards": cards,
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
    settings = session.get(ProjectStudySettings, project_id)
    selected_plan_decks = _selected_plan_decks(session, project_id=project_id)
    if settings is not None and selected_plan_decks:
        return _new_plan_today(
            session,
            user_id=user_id,
            project_id=project_id,
            project=project,
            settings=settings,
            timezone=timezone,
            study_date=study_date,
            day_start=day_start,
            day_end=day_end,
            now=now,
            now_dt=now_dt,
        )
    # A project without the new deck-scoped plan must not silently turn every deck into today's
    # queue.  Keep the legacy chapter plan only when it has an explicit chapter scope; newly
    # created projects therefore show the honest "set a study plan" empty state.
    legacy_scope = False
    if settings is not None:
        try:
            legacy_scope = bool(json.loads(settings.selected_chapter_ids)) or bool(
                settings.include_unassigned
            )
        except (ValueError, TypeError):
            legacy_scope = bool(settings.include_unassigned)
    # ``settings is None`` identifies a pre-plan project created before the deck-scoped settings
    # row was introduced.  Keep that old queue for compatibility; all newly created projects
    # receive an explicit empty settings row above and therefore show the honest unconfigured
    # state until the user saves a plan.
    if settings is not None and not legacy_scope:
        return {
            "timezone": timezone,
            "study_date": study_date,
            "current_project": project_view(session, project),
            "daily_goal": 0,
            "today_completed_count": 0,
            "due_count": 0,
            "main_plan_remaining": 0,
            "backlog_count": 0,
            "plan_configured": False,
            "cards": [],
        }
    deck_ids = list(
        session.scalars(
            select(Deck.deck_id).where(Deck.project_id == project_id, Deck.user_id == user_id)
        ).all()
    )
    due_cards, due_count = _due_plan_cards(
        session,
        user_id=user_id,
        deck_ids=deck_ids,
        now=now,
        now_dt=now_dt,
        goal=daily_goal,
    )
    new_cards = _new_fill_cards(
        session,
        user_id=user_id,
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


def get_study_plan(session: Session, *, user_id: str, now: str) -> dict[str, object]:
    """读取当前用户可编辑的卡组计划。"""
    now_dt = _parse_utc(now)
    prefs = get_preferences(session, user_id=user_id, now=now_dt)
    project_id = cast(str | None, prefs["current_project_id"])
    if project_id is None:
        return {
            "configured": False,
            "current_project_id": None,
            "selected_deck_ids": [],
            "daily_new_goal": 10,
            "daily_review_goal": 40,
            "updated_at": None,
        }
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    settings = session.get(ProjectStudySettings, project_id)
    selected = _selected_plan_decks(session, project_id=project_id)
    return {
        "configured": bool(settings and selected),
        "current_project_id": project.project_id,
        "selected_deck_ids": selected,
        "daily_new_goal": int(settings.daily_new_goal) if settings else 10,
        "daily_review_goal": int(settings.daily_review_goal) if settings else 40,
        "updated_at": settings.updated_at if settings else None,
    }


def _validate_plan_goal(value: int, field: str) -> None:
    if value < 0 or value > 200 or value % 10 != 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"{field} 须为 0~200 的 10 倍数")


def update_study_plan(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    selected_deck_ids: list[str],
    daily_new_goal: int,
    daily_review_goal: int,
    now: str,
) -> dict[str, object]:
    """原子更新当前项目和项目卡组计划。"""
    _owned_project(session, user_id=user_id, project_id=project_id)
    _validate_plan_goal(daily_new_goal, "每日新学目标")
    _validate_plan_goal(daily_review_goal, "每日巩固目标")
    if daily_new_goal + daily_review_goal == 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "每日新学和巩固目标不能同时为 0")
    unique_ids = list(dict.fromkeys(selected_deck_ids))
    if not unique_ids:
        raise AppError(ErrorCode.VALIDATION_ERROR, "至少选择一个卡组")
    decks = list(
        session.scalars(
            select(Deck).where(
                Deck.user_id == user_id,
                Deck.project_id == project_id,
                Deck.deck_id.in_(unique_ids),
            )
        ).all()
    )
    if {deck.deck_id for deck in decks} != set(unique_ids):
        raise AppError(ErrorCode.DECK_NOT_FOUND, "所选卡组不存在或不属于当前项目")
    eligible = session.scalar(
        select(func.count(Card.card_id))
        .where(
            Card.user_id == user_id,
            Card.deck_id.in_(unique_ids),
            text(VISIBLE_PREDICATE_SQL),
        )
    ) or 0
    if eligible == 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "所选卡组暂无可学习卡片")
    now_dt = _parse_utc(now)
    prefs = session.get(UserPreferences, user_id)
    if prefs is None:
        get_preferences(session, user_id=user_id, now=now_dt)
        prefs = session.get(UserPreferences, user_id)
    if prefs is None:  # pragma: no cover - defensive after get-or-create
        raise AppError(ErrorCode.INTERNAL_ERROR, "用户偏好初始化失败")
    prefs.current_project_id = project_id
    prefs.updated_at = now
    settings = session.get(ProjectStudySettings, project_id)
    if settings is None:
        settings = ProjectStudySettings(
            project_id=project_id,
            selected_chapter_ids="[]",
            include_unassigned=0,
            daily_new_goal=daily_new_goal,
            daily_review_goal=daily_review_goal,
            updated_at=now,
        )
        session.add(settings)
        session.flush()
    else:
        settings.daily_new_goal = daily_new_goal
        settings.daily_review_goal = daily_review_goal
        settings.updated_at = now
    # Replace the association set in the same transaction even if an inconsistent database had
    # relation rows without a settings row.  The endpoint therefore has one all-or-nothing plan
    # write rather than appending duplicate/stale deck selections.
    session.execute(delete(ProjectStudyDeck).where(ProjectStudyDeck.project_id == project_id))
    for deck_id in unique_ids:
        session.add(ProjectStudyDeck(project_id=project_id, deck_id=deck_id, created_at=now))
    session.flush()
    return get_study_plan(session, user_id=user_id, now=now)


def study_plan_backlog(
    session: Session,
    *,
    user_id: str,
    now: str,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, object]:
    """读取超过巩固目标的到期卡，供用户主动继续巩固。"""
    if offset < 0 or limit < 1 or limit > 200:
        raise AppError(ErrorCode.VALIDATION_ERROR, "积压分页参数无效")
    plan = get_study_plan(session, user_id=user_id, now=now)
    project_id = cast(str | None, plan["current_project_id"])
    selected = cast(list[str], plan["selected_deck_ids"])
    if project_id is None or not selected:
        return {"items": [], "offset": offset, "limit": limit, "total": 0}
    now_dt = _parse_utc(now)
    prefs = get_preferences(session, user_id=user_id, now=now_dt)
    timezone = str(prefs["learning_timezone"])
    day_start, day_end = day_bounds_utc(now, timezone)
    _new_completed, review_completed = _today_completed_by_kind(
        session,
        user_id=user_id,
        deck_ids=selected,
        day_start=day_start,
        day_end=day_end,
    )
    settings = session.get(ProjectStudySettings, project_id)
    goal = int(settings.daily_review_goal) if settings else 40
    # 重新取完整队列再切掉核心目标，避免将 risk 排序逻辑复制成第二套。
    all_rows = list(
        session.execute(
            select(Card, ReviewState)
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id.in_(selected),
                ReviewState.state != "NEW",
                ReviewState.due <= now,
                text(VISIBLE_PREDICATE_SQL),
            )
        ).all()
    )
    all_rows.sort(
        key=lambda row: (
            -forgetting_risk(row[1].stability, row[1].last_review, now_dt),
            -_overdue_seconds(now_dt, row[1].due),
            row[0].card_id,
        )
    )
    due_count = len(all_rows)
    # Core slots already completed today are consumed even though those cards may no longer be
    # due.  Slice after the remaining core slots so the optional backlog stays addressable after
    # the user finishes the first 40-card queue.
    overflow = all_rows[max(0, goal - review_completed) :]
    items = []
    for card, rs in overflow[offset : offset + limit]:
        view = _plan_card_view(card, rs, now_dt)
        view["plan_kind"] = "DUE"
        items.append(view)
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "total": max(0, due_count - max(0, goal - review_completed)),
    }
