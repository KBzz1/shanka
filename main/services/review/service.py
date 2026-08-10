"""services.review：到期队列 + 评级事务（review_event 插入 + review_state 快照 + client_event_id 兜底）。

事务语义：本模块不 commit/rollback，调用方控制；review_event 与 review_state 更新同事务（database-design §3）。
双幂等（1.3）：Idempotency-Key 层由 handler 的 execute_idempotent 处理（Task 3）；
本模块负责 client_event_id 兜底（UNIQUE(device_id, client_event_id) 冲突 → 比对 → 重放/409；
并发由 BEGIN IMMEDIATE 串行化，先查模式在单写者下足够）。

py-fsrs 4.1.2 事实（R-13 落地，Task 1 校准）：
- Card() = Learning step 0 新卡；无 State.New——ORM "NEW" 初始行（V1）等价 Learning step 0 卡；
- Card 无 reps/lapses 属性——本模块自计数（每次评级 reps +1；AGAIN 时 lapses +1）；
- state 落库用 State 枚举 .name 大写（LEARNING/REVIEW/RELEARNING，契约 3.10 枚举值域），
  构造时由大写反映射回 fsrs State（裁决 1）；
- 契约 3.10 无 step 字段——Learning 卡重建时由 due - last_review 间隔 + last_rating 推导（裁决 2 + I-1）。

now 入参格式契约（M-3）：必须为 `infra.db.session.format_utc` 输出
（`%Y-%m-%dT%H:%M:%S.%fZ`，UTC、恒 3 位毫秒，database-design §0），`_parse_utc` 按此解析。
"""

import uuid
from datetime import UTC, datetime, timedelta

from fsrs import Card as FsrsCard
from fsrs import Rating, State
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Card, ReviewEvent, ReviewState
from infra.db.session import format_utc
from services.cards.service import card_view
from services.decks.service import _owned
from services.scheduling.scheduler import create_scheduler, rating_from_str, review_card

# 初始 NEW 排程（V1 已插行；防御性兜底值）
_INITIAL_STATE = "NEW"
_INITIAL_STABILITY = 0.0
_INITIAL_DIFFICULTY = 1.0

_DUE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _uuid4() -> str:
    return str(uuid.uuid4())


def _parse_utc(value: str) -> datetime:
    """format_utc 输出（database-design §0 恒 3 位毫秒 Z）→ aware UTC datetime。"""
    return datetime.strptime(value, _DUE_FORMAT).replace(tzinfo=UTC)


def _get_review_state(session: Session, *, card_id: str, now: str) -> ReviewState:
    rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
    if rs is None:
        # 防御性：无初始行时按 NEW 构造（V1 创建卡时已插初始行——正常路径有行）
        rs = ReviewState(
            review_state_id=_uuid4(),
            card_id=card_id,
            state=_INITIAL_STATE,
            stability=_INITIAL_STABILITY,
            difficulty=_INITIAL_DIFFICULTY,
            due=now,
            reps=0,
            lapses=0,
            updated_at=now,
        )
        session.add(rs)
    return rs


def _derive_learning_step(rs: ReviewState) -> int:
    """Learning 卡重建时由 due - last_review 间隔 + last_rating 推导 step（裁决 2 + I-1）。

    匹配 learning_steps=[10m, 10m, 1d]（R-13 3 步配置），实证：首 GOOD 后 step=1、间隔 10m；
    二次 GOOD 后 step=2、间隔 1d。10m 间隔歧义（AGAIN/HARD 亦产 10m 间隔、step 0）由
    last_rating 消歧（I-1）：AGAIN/HARD → step 0（重学步起点）；GOOD → step 1。
    1d → step 2。last_review 为空（不应出现于 LEARNING 行）→ 兜底 step 0。
    残余歧义：HARD 保步（step 1 时亦 10m 间隔）重建为 step 0——学习节奏略收紧，登记见报告。
    """
    if rs.last_review is None:
        return 0
    interval = _parse_utc(rs.due) - _parse_utc(rs.last_review)
    if interval == timedelta(days=1):
        return 2
    if rs.last_rating in ("AGAIN", "HARD"):
        return 0
    return 1


def _to_fsrs_card(rs: ReviewState) -> FsrsCard:
    """ReviewState 快照 → py-fsrs Card（构造口径见模块 docstring）。

    NEW：stability/difficulty 传 None 走 fsrs 首评初始化（裁决 3）——快照占位值 0.0/1.0 不可直接输入
    （stability=0.0 + last_review=None → retrievability=0 → _next_stability log(0) ValueError）。
    LEARNING：由 _derive_learning_step 重建 step（裁决 2）。
    RELEARNING：relearning_steps 单步（10m）step 恒为 0，须显式传——fsrs 仅对 Learning 默认
    step=0，Relearning step=None 会在 review_card 断言失败。
    """
    if rs.state == "NEW":
        return FsrsCard(state=State.Learning, due=_parse_utc(rs.due), last_review=None)
    kwargs: dict[str, object] = {
        "stability": rs.stability,
        "difficulty": rs.difficulty,
        "due": _parse_utc(rs.due),
        "last_review": _parse_utc(rs.last_review) if rs.last_review else None,
    }
    if rs.state == "LEARNING":
        kwargs["state"] = State.Learning
        kwargs["step"] = _derive_learning_step(rs)
    elif rs.state == "REVIEW":
        kwargs["state"] = State.Review
    else:  # RELEARNING
        kwargs["state"] = State.Relearning
        kwargs["step"] = 0
    return FsrsCard(**kwargs)


def review_state_view(rs: ReviewState) -> dict[str, object]:
    return {
        "review_state_id": rs.review_state_id,
        "card_id": rs.card_id,
        "state": rs.state,
        "stability": rs.stability,
        "difficulty": rs.difficulty,
        "due": rs.due,
        "last_review": rs.last_review,
        "reps": rs.reps,
        "lapses": rs.lapses,
        "last_rating": rs.last_rating,
        "updated_at": rs.updated_at,
    }


def review_queue(
    session: Session, *, device_id: str, deck_id: str, now: str
) -> list[dict[str, object]]:
    """到期队列（5.15/6.6）：due <= now 按 due、position 稳定排序；返回 {**card_view, review_state}。"""
    _owned(session, device_id=device_id, deck_id=deck_id)
    rows = session.execute(
        select(Card, ReviewState)
        .join(ReviewState, ReviewState.card_id == Card.card_id)
        .where(Card.deck_id == deck_id, ReviewState.due <= now)
        .order_by(ReviewState.due, Card.position)
    ).all()
    return [{**card_view(card), "review_state": review_state_view(rs)} for card, rs in rows]


def _submit_review_inner(
    session: Session,
    *,
    device_id: str,
    card_id: str,
    rating_value: str,
    client_event_id: str,
    device_timezone: str,
    now: str,
) -> tuple[bool, dict[str, object]]:
    """执行评级（幂等原语 fn 内）：返回 (是否因 client_event_id 兜底重放, 响应视图)。"""
    card = session.get(Card, card_id)
    if card is None or card.device_id != device_id:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "卡片不存在")
    rating = rating_from_str(rating_value)  # 非法 → REVIEW_EVENT_INVALID（400）
    rs = _get_review_state(session, card_id=card_id, now=now)

    # client_event_id 兜底（1.3）：先查已有事件（UNIQUE(device_id, client_event_id)）
    existing = session.scalar(
        select(ReviewEvent).where(
            ReviewEvent.device_id == device_id,
            ReviewEvent.client_event_id == client_event_id,
        )
    )
    if existing is not None:
        if existing.card_id == card_id and existing.rating == rating_value:
            return True, review_state_view(rs)  # 重放：读当前 review_state 视图（R-12 口径）
        raise AppError(ErrorCode.REVIEW_EVENT_CONFLICT, "client_event_id 已用于其他评级")

    # review_datetime 固定为事务时钟（C-02 确定性；fsrs 要求 aware UTC）
    new_card, _ = review_card(
        create_scheduler(), _to_fsrs_card(rs), rating, review_datetime=_parse_utc(now)
    )

    # 更新 ReviewState 全量快照（database-design 2.10）；reps/lapses 自计数（4.x Card 无此属性）
    # state 落库大写（契约 3.10 枚举 NEW/LEARNING/REVIEW/RELEARNING，裁决 1）
    rs.state = new_card.state.name.upper()
    rs.stability = float(new_card.stability)
    rs.difficulty = float(new_card.difficulty)
    rs.due = format_utc(new_card.due)
    rs.last_review = now
    rs.reps += 1
    if rating == Rating.Again:
        rs.lapses += 1
    rs.last_rating = rating_value
    rs.updated_at = now

    # review_event 不可变记录（3.11）
    session.add(
        ReviewEvent(
            review_event_id=_uuid4(),
            device_id=device_id,
            card_id=card_id,
            client_event_id=client_event_id,
            rating=rating_value,
            reviewed_at=now,
            device_timezone=device_timezone,
            created_at=now,
        )
    )
    return False, review_state_view(rs)


def submit_review(
    session: Session,
    *,
    device_id: str,
    card_id: str,
    rating: str,
    client_event_id: str,
    device_timezone: str,
    now: str,
) -> dict[str, object]:
    """评级事务入口（handler 层再包 execute_idempotent，Task 3）。"""
    _, view = _submit_review_inner(
        session,
        device_id=device_id,
        card_id=card_id,
        rating_value=rating,
        client_event_id=client_event_id,
        device_timezone=device_timezone,
        now=now,
    )
    return view
