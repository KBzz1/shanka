"""services.stats：看板聚合（structure-contract 3.12/PRD 5.16；database-design §4 直接基于 review_events 聚合）。

口径（R-12 裁决，登记 Progress）：
- 周活动/周总数/周变化率/周目标完成率/回忆正确率/记忆保持率：当前自然周（周一）按上报 timezone 分桶；
- 首次答对率：每卡历史首个事件为 GOOD 的比例（契约字面无周期限定，累计口径）；
- 连续学习天数：按本次上报 timezone 截至本地当天连续有事件的自然日数；
- 已掌握：C-03（REVIEW 且 stability>=21）去重卡数（全量）；
- 分母 0 的比率一律 None（PRD 5.16）。
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Card, ReviewEvent, ReviewState

_WEEKDAYS = 7


def _week_bounds(tz: ZoneInfo, now: datetime) -> tuple[datetime, datetime]:
    """当前自然周（周一 00:00 ~ 下周一 00:00，本地时区）。"""
    local = now.astimezone(tz)
    monday = local - timedelta(days=local.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=7)


def _parse_tz(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"非法 IANA 时区: {timezone_name}") from exc


def _as_utc_str(dt: datetime) -> str:
    from infra.db.session import format_utc

    return format_utc(dt.astimezone(UTC))


def dashboard(
    session: Session, *, device_id: str, timezone: str, weekly_goal: int | None, now: datetime
) -> dict[str, object]:
    tz = _parse_tz(timezone)
    start, end = _week_bounds(tz, now)
    start_str, end_str = _as_utc_str(start), _as_utc_str(end)

    # 周内事件（按 reviewed_at 字符串比较——统一格式字典序=时间序）
    week_events = session.scalars(
        select(ReviewEvent).where(
            ReviewEvent.device_id == device_id,
            ReviewEvent.reviewed_at >= start_str,
            ReviewEvent.reviewed_at < end_str,
        )
    ).all()
    # 上周事件（start 已是本地周一 00:00 → 上周 = [start-7d, start)，不再过 _week_bounds）
    last_start, last_end = start - timedelta(days=7), start
    last_week_count = (
        session.scalar(
            select(func.count(ReviewEvent.review_event_id)).where(
                ReviewEvent.device_id == device_id,
                ReviewEvent.reviewed_at >= _as_utc_str(last_start),
                ReviewEvent.reviewed_at < _as_utc_str(last_end),
            )
        )
        or 0
    )

    weekly_activity = [0] * _WEEKDAYS
    for ev in week_events:
        local = datetime.fromisoformat(ev.reviewed_at).astimezone(tz)
        weekly_activity[local.weekday()] += 1
    weekly_total = sum(weekly_activity)

    # 回忆正确率（周内 GOOD/全部）
    good_week = sum(1 for ev in week_events if ev.rating == "GOOD")
    recall = good_week / weekly_total if weekly_total else None

    # 记忆保持率（周内非首次事件：该卡在本事件前已有更早事件）
    non_first = [ev for ev in week_events if _is_non_first(session, device_id=device_id, ev=ev)]
    retention = (
        sum(1 for ev in non_first if ev.rating == "GOOD") / len(non_first) if non_first else None
    )

    # 首次答对率（每卡历史首个事件为 GOOD）
    first_answer = _first_answer_accuracy(session, device_id=device_id)

    # 周变化率
    week_change = (weekly_total - last_week_count) / last_week_count if last_week_count else None

    # 周目标
    goal_progress = min(weekly_total / weekly_goal, 1.0) if weekly_goal else None

    # 连续学习天数（截至本地当天）
    streak = _streak_days(session, device_id=device_id, tz=tz, now=now)

    # 已掌握（C-03）
    mastered = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.device_id == device_id,
                ReviewState.state == "REVIEW",
                ReviewState.stability >= 21,
            )
        )
        or 0
    )

    return {
        "period": {"start": start_str, "end": end_str, "week_ordinal": start.isocalendar()[1]},
        "timezone": timezone,
        "weekly_activity": weekly_activity,
        "weekly_total": weekly_total,
        "week_change_rate": week_change,
        "weekly_goal": weekly_goal,
        "weekly_goal_progress": goal_progress,
        "recall_accuracy": recall,
        "first_answer_accuracy": first_answer,
        "retention_rate": retention,
        "streak_days": streak,
        "mastered_card_count": mastered,
        "updated_at": _as_utc_str(now),
        "has_data": weekly_total > 0 or mastered > 0,
    }


def _is_non_first(session: Session, *, device_id: str, ev: ReviewEvent) -> bool:
    earlier = session.scalar(
        select(func.count(ReviewEvent.review_event_id)).where(
            ReviewEvent.device_id == device_id,
            ReviewEvent.card_id == ev.card_id,
            ReviewEvent.reviewed_at < ev.reviewed_at,
        )
    )
    return bool(earlier)


def _first_answer_accuracy(session: Session, *, device_id: str) -> float | None:
    """每卡历史首个事件为 GOOD 的比例。"""
    cards_with_events = (
        session.execute(
            select(Card.card_id)
            .join(ReviewEvent, ReviewEvent.card_id == Card.card_id)
            .where(Card.device_id == device_id)
            .distinct()
        )
        .scalars()
        .all()
    )
    if not cards_with_events:
        return None
    first_good = 0
    for card_id in cards_with_events:
        first_event = session.scalar(
            select(ReviewEvent)
            .where(ReviewEvent.device_id == device_id, ReviewEvent.card_id == card_id)
            .order_by(ReviewEvent.reviewed_at, ReviewEvent.created_at)
            .limit(1)
        )
        if first_event is not None and first_event.rating == "GOOD":
            first_good += 1
    return first_good / len(cards_with_events)


def _streak_days(session: Session, *, device_id: str, tz: ZoneInfo, now: datetime) -> int:
    local_today = now.astimezone(tz).date()
    streak = 0
    day = local_today
    while True:
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        count = session.scalar(
            select(func.count(ReviewEvent.review_event_id)).where(
                ReviewEvent.device_id == device_id,
                ReviewEvent.reviewed_at >= _as_utc_str(day_start),
                ReviewEvent.reviewed_at < _as_utc_str(day_end),
            )
        )
        if not count:
            break
        streak += 1
        day -= timedelta(days=1)
    return streak
