"""services.study.sessions：学习会话（structure-contract 3.25/6.6；V25-D-37）。

薄容器语义——**状态在卡上、事实在流水里、会话只是当天的门**：

- begin 按自然键 ``(user_id, study_date, origin, deck_key)`` upsert：同日同来源同范围
  再次进入返回同一行（中断续学仅限当天，响应携带已累计秒数供客户端续算基数）；跨
  学习日自动开新行，不存在跨天续用与僵尸会话清理。deck_key 为 deck_id 或 '*'（跨卡组），
  物化非空以规避 SQLite UNIQUE 对 NULL 不去重。
- 时长上报按客户端累计**绝对值** max 合并：重复/乱序天然幂等，不叠加；越界 0~86400 拒绝。
  时长是尽力送达的观测数据，非评分事实。
- 会话不保存卡片状态、队列或完成数；恢复会话后的学习队列按当前 ReviewState 现算
  （到期队列天然扣除当日已评分卡——评分后 due 被推向未来）。

学习日复用账号学习时区口径（preferences 权威，契约 1.2）：learning_date(now, timezone)，
与今日计划/统计同一分桶，不自造第二套时区换算。

事务语义：本模块不 commit/rollback，调用方控制（与其他 services 一致）。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from domain.enums import ReviewOrigin
from infra.db.models import Card, Deck, ReviewState, StudySession
from services.cards.service import card_view
from services.preferences.service import get_preferences, learning_date
from services.review.service import review_state_view
from services.scheduling.scheduler import forgetting_risk

# database-design §0 时间戳字符串格式（恒 3 位毫秒 Z）
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# PLAN/BACKLOG 跨卡组范围的自然键物化值（deck_key 非 NULL 才能参与 UNIQUE 去重）
_CROSS_DECK_KEY = "*"

_MAX_STUDY_SECONDS = 86400


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def study_session_view(row: StudySession) -> dict[str, object]:
    """StudySession 视图（契约 3.25 字段表）。"""
    return {
        "session_id": row.session_id,
        "origin": row.origin,
        "deck_id": row.deck_id,
        "study_date": row.study_date,
        "study_seconds": int(row.study_seconds),
        "started_at": row.started_at,
        "last_reported_at": row.last_reported_at,
        "ended_at": row.ended_at,
    }


def _origin_and_scope(
    session: Session, *, user_id: str, origin: str, deck_id: str | None
) -> tuple[str, str | None, str]:
    """校验 origin 与范围组合，返回 (origin 值, deck_id, deck_key)。"""
    try:
        origin_value = ReviewOrigin(origin).value
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "origin 须为 PLAN/BACKLOG/ADHOC") from exc
    if origin_value == ReviewOrigin.ADHOC.value:
        if deck_id is None:
            raise AppError(ErrorCode.VALIDATION_ERROR, "ADHOC 会话必须携带 deck_id")
        deck = session.scalar(select(Deck).where(Deck.deck_id == deck_id, Deck.user_id == user_id))
        if deck is None:
            raise AppError(ErrorCode.DECK_NOT_FOUND, "卡组不存在")
        return origin_value, deck_id, deck_id
    if deck_id is not None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "PLAN/BACKLOG 会话不绑定单一卡组")
    return origin_value, None, _CROSS_DECK_KEY


def begin_or_resume(
    session: Session,
    *,
    user_id: str,
    origin: str,
    deck_id: str | None,
    now: str,
    reset: bool = False,
) -> dict[str, object]:
    """开启/续用当日学习会话：自然键命中即返回现值（含累计秒数），否则新建。

    reset=True（V25-D-37 会话重置）：把当日同源同范围的现存会话封存（ended_at 落定、
    时长永久保留在历史），并开启一个从 0 计时的新会话行。唯一键经 deck_key 代数后缀
    ``deck_id#N`` 消解——同一学习日同卡组可以有多代已封存会话 + 至多一代活跃会话；
    续用（reset=False）永远命中活跃行。重置只影响记账与队列，不改写任何评分事实。
    """
    origin_value, deck_id_value, deck_key = _origin_and_scope(
        session, user_id=user_id, origin=origin, deck_id=deck_id
    )
    prefs = get_preferences(session, user_id=user_id, now=_parse_utc(now))
    study_date = learning_date(now, str(prefs["learning_timezone"]))
    scope = (
        StudySession.user_id == user_id,
        StudySession.study_date == study_date,
        StudySession.origin == origin_value,
        StudySession.deck_key.in_([deck_key, *[f"{deck_key}#{n}" for n in range(1, 10)]]),
    )
    # 活跃行优先（含裸键与 #N 代）；无活跃行时裸键行作为 reset 的封存对象。
    row = session.scalar(
        select(StudySession).where(*scope, StudySession.ended_at.is_(None))
    ) or session.scalar(select(StudySession).where(*scope))
    if reset and row is not None and row.ended_at is None:
        row.ended_at = now
        session.flush()
        # 代数 = 已封存代数 + 1；deck_key 加后缀腾出自然键，让新行当日可再建。
        sealed = session.scalar(
            select(func.count(StudySession.session_id)).where(
                StudySession.user_id == user_id,
                StudySession.study_date == study_date,
                StudySession.origin == origin_value,
                StudySession.deck_key.like(f"{deck_key}#%"),
            )
        )
        row = None
        deck_key = f"{deck_key}#{int(sealed or 0) + 1}"
    if row is None:
        row = StudySession(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            origin=origin_value,
            deck_id=deck_id_value,
            deck_key=deck_key,
            study_date=study_date,
            study_seconds=0,
            started_at=now,
        )
        session.add(row)
        session.flush()
    return study_session_view(row)


def report_study_session(
    session: Session,
    *,
    user_id: str,
    session_id: str,
    study_seconds: int,
    ended: bool,
    now: str,
) -> dict[str, object]:
    """上报会话时长：累计绝对值 max 合并（非增量），重复/乱序天然幂等。"""
    if study_seconds < 0 or study_seconds > _MAX_STUDY_SECONDS:
        raise AppError(ErrorCode.VALIDATION_ERROR, "study_seconds 须为 0~86400 的绝对累计值")
    row = session.scalar(
        select(StudySession).where(
            StudySession.session_id == session_id, StudySession.user_id == user_id
        )
    )
    if row is None:
        raise AppError(ErrorCode.SESSION_NOT_FOUND, "学习会话不存在")
    row.study_seconds = max(int(row.study_seconds), study_seconds)
    row.last_reported_at = now
    if ended:
        row.ended_at = now
    return study_session_view(row)


def sessions_of_day(
    session: Session, *, user_id: str, now: str, study_date: str | None
) -> dict[str, object]:
    """查询指定学习日（缺省 = 账号学习时区今天）的会话列表。"""
    if study_date is None:
        prefs = get_preferences(session, user_id=user_id, now=_parse_utc(now))
        study_date = learning_date(now, str(prefs["learning_timezone"]))
    rows = list(
        session.scalars(
            select(StudySession)
            .where(StudySession.user_id == user_id, StudySession.study_date == study_date)
            .order_by(StudySession.started_at, StudySession.session_id)
        ).all()
    )
    return {"study_date": study_date, "items": [study_session_view(row) for row in rows]}


def sessions_summary(session: Session, *, user_id: str) -> dict[str, object]:
    """全历史时长汇总：ADHOC 按卡组 + 三来源合计，供项目/卡组详情的既有「学习时长」卡
    跨设备读取（V25-D-37）；deck 随删除级联，列表只含现存卡组。纯读，无学习日分桶。"""
    deck_rows = session.execute(
        select(StudySession.deck_key, func.sum(StudySession.study_seconds))
        .where(StudySession.user_id == user_id, StudySession.origin == ReviewOrigin.ADHOC.value)
        .group_by(StudySession.deck_key)
        .order_by(StudySession.deck_key)
    ).all()
    origin_rows = session.execute(
        select(StudySession.origin, func.sum(StudySession.study_seconds))
        .where(StudySession.user_id == user_id)
        .group_by(StudySession.origin)
    ).all()
    totals = {origin.value: 0 for origin in ReviewOrigin}
    for origin_value, seconds in origin_rows:
        totals[origin_value] = int(seconds or 0)
    return {
        "deck_study_seconds": [
            {"deck_id": deck_key, "study_seconds": int(seconds or 0)}
            for deck_key, seconds in deck_rows
        ],
        "plan_study_seconds": totals[ReviewOrigin.PLAN.value],
        "backlog_study_seconds": totals[ReviewOrigin.BACKLOG.value],
        "adhoc_study_seconds": totals[ReviewOrigin.ADHOC.value],
    }


def reset_review_queue(
    session: Session, *, user_id: str, deck_id: str, now: str
) -> list[dict[str, object]]:
    """会话重置后的全卡组复盘队列（V25-D-37）：新卡排最前（按 position、card_id），
    其余可见卡按 FSRS 遗忘风险降序（同日复习风险同为 0 时按 due、position 消歧）。

    不做到期过滤——重置的语义是「把这个卡组完整复盘一遍」；每次评分照常走 FSRS
    排程，本查询不改写任何卡片状态。"""
    deck = session.scalar(select(Deck).where(Deck.deck_id == deck_id, Deck.user_id == user_id))
    if deck is None:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "卡组不存在")
    now_dt = _parse_utc(now)
    rows = list(
        session.execute(
            select(Card, ReviewState)
            .outerjoin(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id == deck_id,
                sql_text(VISIBLE_PREDICATE_SQL),
            )
        ).all()
    )
    fresh: list[tuple[Card, ReviewState | None]] = []
    review: list[tuple[Card, ReviewState | None]] = []
    for card, state in rows:
        if state is None or state.state == "NEW":
            fresh.append((card, state))
        else:
            review.append((card, state))

    def position_key(card: Card) -> tuple[object, ...]:
        return (card.position, card.card_id)

    fresh.sort(key=lambda row: position_key(row[0]))
    review.sort(
        key=lambda row: (
            -forgetting_risk(row[1].stability, row[1].last_review, now_dt) if row[1] else 0.0,
            row[1].due if row[1] else "",
            *position_key(row[0]),
        )
    )
    return [
        {**card_view(card), "review_state": None if state is None else review_state_view(state)}
        for card, state in [*fresh, *review]
    ]
