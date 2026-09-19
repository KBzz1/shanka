"""services.study：今日学习计划（structure-contract 3.20/6.6；openapi /study/today，V2.5 新增）。

主计划（4.5）：账号计划所选卡组内已学习（state != NEW）且到期（due <= now）的可见卡 →
按遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序取巩固目标；再从同一批卡组的
NEW 卡按 deck_id、position、card_id 补足新学目标。学习日期与今日去重完成数按账号
IANA 学习时区分桶（UTC reviewed_at 折算，不改写事件；helpers 在 services.preferences.service）。

V25-D-39（账号级跨项目计划，取代 V25-D-03 单项目规则）：计划归属账号而非项目，
``user_study_decks`` 可收录任意本人卡组（跨项目与独立卡组）；保存计划不再改写
``user_preferences.current_project_id``（该字段只由显式 PATCH /preferences 控制）。
账号未保存计划（无 user_study_settings 行或零卡组）只返回未配置空态，不把任何
卡组偷偷加入今日计划；项目级章节回退分支已随迁移退役。

计划容量裁决：巩固目标是软目标，核心队列先取最多目标张到期卡；完成核心队列后，
`/study/today/backlog` 可按同一排序继续读取积压。首页合计目标使用当天真实可供学习的
新卡与到期卡数量，避免到期卡不足时显示无法完成的固定上限。

事务语义：本模块不 commit/rollback，调用方控制；today 的 get-or-create（偏好默认行）
为物化写，由 handler 提交。
"""

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import (
    Card,
    Deck,
    ReviewEvent,
    ReviewState,
    UserStudyDeck,
    UserStudySettings,
)
from services.cards.service import card_view
from services.preferences.service import day_bounds_utc, get_preferences, learning_date
from services.review.service import review_state_view
from services.scheduling.scheduler import forgetting_risk

# database-design §0 时间戳字符串格式（恒 3 位毫秒 Z）
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def _overdue_seconds(now_dt: datetime, due: str) -> float:
    """逾期时长（秒）：now - due（due <= now 过滤后非负）。"""
    return (now_dt - _parse_utc(due)).total_seconds()


def _due_queue(
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    now: str,
    now_dt: datetime,
) -> list[tuple[Card, ReviewState]]:
    """已学习且到期可见卡（state != NEW、due <= now）全量队列，排序键：(-遗忘风险, -逾期时长, card_id)。

    主计划与 backlog 共用同一排序来源，避免 risk 排序长成第二套实现。
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
    rows.sort(
        key=lambda row: (
            -forgetting_risk(row[1].stability, row[1].last_review, now_dt),
            -_overdue_seconds(now_dt, row[1].due),
            row[0].card_id,
        )
    )
    return [(row[0], row[1]) for row in rows]


def _due_plan_cards(
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    now: str,
    now_dt: datetime,
    goal: int,
) -> tuple[list[tuple[Card, ReviewState]], int]:
    """已学习且到期可见卡 → 排序 → 取到每日目标。

    返回 (计划内到期卡, 到期总数)。
    """
    queue = _due_queue(session, user_id=user_id, deck_ids=deck_ids, now=now, now_dt=now_dt)
    return queue[:goal], len(queue)


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


def _selected_plan_decks(session: Session, *, user_id: str) -> list[str]:
    return list(
        session.scalars(
            select(UserStudyDeck.deck_id)
            .where(UserStudyDeck.user_id == user_id)
            .order_by(UserStudyDeck.created_at, UserStudyDeck.deck_id)
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
    """返回今日已完成的新卡数、巩固卡数（同卡同日去重）。

    只取窗口内事件 + 今日活跃卡片的全量首条评分：新卡/巩固的判定只需要今天活跃
    卡片各自的第一条事件，而全历史扫描会让每次 /study/today 的内存与延迟随账号
    历史线性放大。
    """
    if not deck_ids:
        return 0, 0
    visible = text(VISIBLE_PREDICATE_SQL)
    card_join = (Card.card_id == ReviewEvent.card_id) & (Card.user_id == user_id)
    today_rows = list(
        session.execute(
            select(ReviewEvent.review_event_id, ReviewEvent.card_id)
            .join(Card, card_join)
            .where(
                ReviewEvent.user_id == user_id,
                Card.deck_id.in_(deck_ids),
                visible,
                ReviewEvent.reviewed_at >= day_start,
                ReviewEvent.reviewed_at < day_end,
            )
        ).all()
    )
    if not today_rows:
        return 0, 0
    history = session.execute(
        select(ReviewEvent.review_event_id, ReviewEvent.card_id)
        .join(Card, card_join)
        .where(
            ReviewEvent.user_id == user_id,
            Card.deck_id.in_(deck_ids),
            visible,
            ReviewEvent.card_id.in_({row.card_id for row in today_rows}),
        )
        .order_by(ReviewEvent.card_id, ReviewEvent.reviewed_at, ReviewEvent.created_at)
    ).all()
    # Keep the event id rather than only reviewed_at: two ratings can legitimately share the
    # same millisecond, and timestamp equality alone would classify both as a card's first event.
    first_seen: dict[str, str] = {}
    for row in history:
        first_seen.setdefault(row.card_id, row.review_event_id)
    new_cards: set[str] = set()
    review_cards: set[str] = set()
    for row in today_rows:
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
    settings: UserStudySettings,
    timezone: str,
    study_date: str,
    day_start: str,
    day_end: str,
    now: str,
    now_dt: datetime,
) -> dict[str, object]:
    deck_ids = _selected_plan_decks(session, user_id=user_id)
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
    """账号今日计划（3.20）：未保存计划时空态（plan_configured=false）。"""
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
    settings = session.get(UserStudySettings, user_id)
    selected_plan_decks = _selected_plan_decks(session, user_id=user_id)
    if settings is None or not selected_plan_decks:
        # A user without a saved plan gets the honest "set a study plan" empty state —
        # no deck ever joins today's queue implicitly.
        return {
            "timezone": timezone,
            "study_date": study_date,
            "daily_goal": daily_goal,
            "today_completed_count": completed,
            "due_count": 0,
            "main_plan_remaining": 0,
            "backlog_count": 0,
            "plan_configured": False,
            "selected_deck_ids": [],
            "cards": [],
        }
    return _new_plan_today(
        session,
        user_id=user_id,
        settings=settings,
        timezone=timezone,
        study_date=study_date,
        day_start=day_start,
        day_end=day_end,
        now=now,
        now_dt=now_dt,
    )


def get_study_plan(session: Session, *, user_id: str) -> dict[str, object]:
    """读取当前用户可编辑的账号级卡组计划。"""
    settings = session.get(UserStudySettings, user_id)
    selected = _selected_plan_decks(session, user_id=user_id)
    return {
        "configured": bool(settings and selected),
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
    selected_deck_ids: list[str],
    daily_new_goal: int,
    daily_review_goal: int,
    now: str,
) -> dict[str, object]:
    """原子更新账号卡组计划（V25-D-39：可跨项目与独立卡组，不触碰当前项目）。"""
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
                Deck.deck_id.in_(unique_ids),
            )
        ).all()
    )
    if {deck.deck_id for deck in decks} != set(unique_ids):
        raise AppError(ErrorCode.DECK_NOT_FOUND, "所选卡组不存在或已删除")
    eligible = (
        session.scalar(
            select(func.count(Card.card_id)).where(
                Card.user_id == user_id,
                Card.deck_id.in_(unique_ids),
                text(VISIBLE_PREDICATE_SQL),
            )
        )
        or 0
    )
    if eligible == 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "所选卡组暂无可学习卡片")
    settings = session.get(UserStudySettings, user_id)
    if settings is None:
        settings = UserStudySettings(
            user_id=user_id,
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
    session.execute(delete(UserStudyDeck).where(UserStudyDeck.user_id == user_id))
    for deck_id in unique_ids:
        session.add(UserStudyDeck(user_id=user_id, deck_id=deck_id, created_at=now))
    session.flush()
    return get_study_plan(session, user_id=user_id)


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
    plan = get_study_plan(session, user_id=user_id)
    selected = cast(list[str], plan["selected_deck_ids"])
    if not plan["configured"] or not selected:
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
    settings = session.get(UserStudySettings, user_id)
    goal = int(settings.daily_review_goal) if settings else 40
    # 与主计划共用同一排序来源（_due_queue），再切掉核心目标，避免 risk 排序复制成第二套。
    all_rows = _due_queue(session, user_id=user_id, deck_ids=selected, now=now, now_dt=now_dt)
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
