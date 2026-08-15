"""services.stats：看板聚合（structure-contract 3.12/PRD 5.16；database-design §4 直接基于 review_events 聚合）。

V2.5 口径（Task 11 实现）：
- 全部周口径（周活动/周总数/周变化率/周完成/回忆正确率/记忆保持率/streak/周界）按
  账号学习时区（preferences.learning_timezone，契约 1.2）分桶：UTC reviewed_at 折算，
  不改写事件（时区改变后重新分桶）；学习日期换算复用 services.preferences.service
  learning_date——统计不得自造第二套时区换算。
- 周目标 = daily_learning_goal × 7（服务端派生；不再接受客户端 timezone/weekly_goal）。
- 首次答对率：每卡历史首个事件为 GOOD 的比例（契约字面无周期限定，累计口径）。
- 已掌握：C-03（REVIEW 且 stability>=21）去重卡数（全量）。
- 统计源 = review_events（评级事件）：自由刷题不写事件不计统计；事件按卡片当前可见性
  （统一可见谓词 domain/card.py）过滤——STAGED/删除批次卡的事件不进入任何口径
  （删除批次最终清理级联删事件，撤销窗口内也不保留幽灵计数）。
- 分母 0 的比率一律 None（PRD 5.16）；无数据时不伪造固定日期数组或伪 0%。

性能（database-design §4 基线：千级卡片、万级事件）：周窗口一次区间查询（索引
ix_review_events_user_reviewed 覆盖）+ 全历史一次有序遍历（集合式，无 N+1），
已掌握为单条 COUNT；实测见 tests/integration/test_v25_stats_performance.py。
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import Card, ReviewEvent, ReviewState
from infra.db.session import format_utc
from services.preferences.service import get_preferences, learning_date

_WEEKDAYS = 7


def _account_tz(timezone_name: str) -> ZoneInfo:
    """账号学习时区（写路径已校验 INVALID_LEARNING_TIMEZONE；防御性兜底：损坏偏好不得 500）。"""
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError(
            ErrorCode.INVALID_LEARNING_TIMEZONE, f"无效的学习时区: {timezone_name}"
        ) from exc


def _week_bounds(timezone: str, now: datetime) -> tuple[datetime, datetime]:
    """当前自然周（周一 00:00 ~ 下周一 00:00，账号学习时区）。

    学习日期经 learning_date 折算（复用偏好服务换算），周一算术只做本地日期偏移。
    """
    local_date = date.fromisoformat(learning_date(format_utc(now), timezone))
    monday = local_date - timedelta(days=local_date.weekday())
    start = datetime(monday.year, monday.month, monday.day, tzinfo=ZoneInfo(timezone))
    return start, start + timedelta(days=7)


def dashboard(session: Session, *, user_id: str, now: datetime) -> dict[str, object]:
    prefs = get_preferences(session, user_id=user_id, now=now)
    timezone = prefs["learning_timezone"]
    _account_tz(timezone)
    weekly_goal = prefs["daily_learning_goal"] * 7

    start, end = _week_bounds(timezone, now)
    start_str, end_str = format_utc(start), format_utc(end)
    last_start = start - timedelta(days=7)

    # 周窗口（上周+本周）事件：join 可见卡（统一可见谓词），user+reviewed_at 索引区间。
    # 不可见卡（STAGED/删除批次）事件不进入任何统计口径。
    rows = session.execute(
        select(
            ReviewEvent.card_id,
            ReviewEvent.rating,
            ReviewEvent.reviewed_at,
        )
        .join(
            Card,
            (Card.card_id == ReviewEvent.card_id) & (Card.user_id == ReviewEvent.user_id),
        )
        .where(
            ReviewEvent.user_id == user_id,
            ReviewEvent.reviewed_at >= format_utc(last_start),
            ReviewEvent.reviewed_at < end_str,
            text(VISIBLE_PREDICATE_SQL),
        )
    ).all()
    # format_utc 恒 3 位毫秒 Z——字典序 = 时间序（database-design §0）
    last_week = [r for r in rows if r.reviewed_at < start_str]
    week = [r for r in rows if r.reviewed_at >= start_str]

    weekly_activity = [0] * _WEEKDAYS
    for r in week:
        local = date.fromisoformat(learning_date(r.reviewed_at, timezone))
        weekly_activity[local.weekday()] += 1
    weekly_total = len(week)

    # 回忆正确率（周内 GOOD/全部）
    recall = sum(1 for r in week if r.rating == "GOOD") / weekly_total if weekly_total else None

    # 周变化率：上周为 0 时 null（客户端显示"暂无对比"）
    last_week_count = len(last_week)
    week_change = (weekly_total - last_week_count) / last_week_count if last_week_count else None

    # 周完成：本周不同 (账号学习日期, card_id) 数（去重；同日同卡多次评级只计 1）
    weekly_completed_count = len(
        {(learning_date(r.reviewed_at, timezone), r.card_id) for r in week}
    )

    # 周目标完成率：min(weekly_completed_count / weekly_goal, 1)
    goal_progress = min(weekly_completed_count / weekly_goal, 1.0) if weekly_goal else None

    # 首事件遍历（全历史，可见卡，一次有序查询）：每卡首个事件 = 组内最小
    # (reviewed_at, created_at)（ORDER BY 组序保证）；非首次 = 存在严格更早
    # reviewed_at（同毫秒事件不互判非首次，与历史口径一致）。
    walk = session.execute(
        select(
            ReviewEvent.card_id,
            ReviewEvent.rating,
            ReviewEvent.reviewed_at,
        )
        .join(
            Card,
            (Card.card_id == ReviewEvent.card_id) & (Card.user_id == ReviewEvent.user_id),
        )
        .where(ReviewEvent.user_id == user_id, text(VISIBLE_PREDICATE_SQL))
        .order_by(ReviewEvent.card_id, ReviewEvent.reviewed_at, ReviewEvent.created_at)
    ).all()
    first_rating: dict[str, str] = {}
    min_reviewed_at: dict[str, str] = {}
    learning_days: set[str] = set()  # streak：有可见卡事件的学习日
    for r in walk:
        if r.card_id not in min_reviewed_at:
            first_rating[r.card_id] = r.rating
            min_reviewed_at[r.card_id] = r.reviewed_at
        learning_days.add(learning_date(r.reviewed_at, timezone))

    # 记忆保持率（周内非首次事件 GOOD 占比）
    non_first = [r for r in week if r.reviewed_at > min_reviewed_at[r.card_id]]
    retention = (
        sum(1 for r in non_first if r.rating == "GOOD") / len(non_first) if non_first else None
    )

    # 首次答对率（每卡历史首个事件为 GOOD 的比例）
    first_answer = (
        sum(1 for rating in first_rating.values() if rating == "GOOD") / len(first_rating)
        if first_rating
        else None
    )

    # 连续学习天数（截至账号学习时区当天；只计可见卡事件日）
    today = date.fromisoformat(learning_date(format_utc(now), timezone))
    streak = 0
    while today.isoformat() in learning_days:
        streak += 1
        today -= timedelta(days=1)

    # 已掌握（C-03；只含可见卡——统一可见谓词 3.9）
    mastered = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                ReviewState.state == "REVIEW",
                ReviewState.stability >= 21,
                text(VISIBLE_PREDICATE_SQL),
            )
        )
        or 0
    )

    return {
        "period": {"start": start_str, "end": end_str, "week_ordinal": start.isocalendar()[1]},
        "timezone": timezone,
        "weekly_activity": weekly_activity,
        "weekly_total": weekly_total,
        "weekly_completed_count": weekly_completed_count,
        "week_change_rate": week_change,
        "weekly_goal": weekly_goal,
        "weekly_goal_progress": goal_progress,
        "recall_accuracy": recall,
        "first_answer_accuracy": first_answer,
        "retention_rate": retention,
        "streak_days": streak,
        "mastered_card_count": mastered,
        "updated_at": format_utc(now),
        "has_data": weekly_total > 0 or mastered > 0,
    }
