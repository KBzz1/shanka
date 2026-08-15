"""services.cards.deletion：10 秒撤销删除批次（structure-contract 3.18/6.5；FR-04/D-19/D-24）。

- 删除 = 可见性标记 + 批次状态（服务端计时），非客户端计时硬删；卡行不动 →
  撤销天然完整恢复内容、原位置、复习状态与历史学习记录（FR-04）。
- 追加合并：携带 delete_batch_id 且对应批仍 PENDING → 原子刷新整批
  undo_until = now + 10s（3.18），成员卡 undo_until 同步；过期/非 PENDING 批
  不再可合并 → 自动新建批。
- 撤销：窗口内（undo_until > now）同一事务清空所有卡片删除标记并置 UNDONE；
  过期/终态批 → 409 CARD_DELETE_WINDOW_EXPIRED（跨用户 → 404）。
- 惰性清理：任意相关读取/写入前先 finalize 过期批——硬删除全部卡（FK 级联
  review_states/review_events）并置 FINALIZED；重跑不再命中（幂等，3.18 两种
  路径都必须幂等）。V2.5 无回收站。
- 事务归 services：本模块不 commit，由调用方（handler 幂等包装）控制。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.deletion_batch import UNDO_WINDOW_SECONDS
from infra.db.models import Card, CardDeletionBatch
from infra.db.session import format_utc
from services.cards.service import _owned_card


def _parse_utc(value: str) -> datetime:
    """反解 format_utc 输出（固定形如 %Y-%m-%dT%H:%M:%S.%fZ 的 UTC 字符串；
    Python 3.11+ fromisoformat 原生接受 Z 后缀）。"""
    return datetime.fromisoformat(value)


def _now_plus_window(now: str) -> str:
    return format_utc(_parse_utc(now) + timedelta(seconds=UNDO_WINDOW_SECONDS))


def _batch_cards(session: Session, *, batch_id: str) -> list[Card]:
    """批内卡（数据库以 Cards 外键关系为权威，3.18）：按加入顺序稳定排序。"""
    return list(
        session.scalars(
            select(Card)
            .where(Card.delete_batch_id == batch_id)
            .order_by(Card.pending_delete_at, Card.card_id)
        ).all()
    )


def _batch_view(session: Session, batch: CardDeletionBatch) -> dict[str, object]:
    return {
        "delete_batch_id": batch.delete_batch_id,
        "card_ids": [c.card_id for c in _batch_cards(session, batch_id=batch.delete_batch_id)],
        "undo_until": batch.undo_until,
        "status": batch.status,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def finalize_expired_batches(session: Session, *, user_id: str, now: str) -> int:
    """惰性清理（3.18）：过期 PENDING 批硬删除全部卡并置 FINALIZED，返回处理的批数。

    幂等：重复执行不再命中任何过期批 → 0。调用点：删除追加、pending 检索、撤销
    （任意相关读取前的惰性清理路径；本后端无后台清理器）。
    """
    finalized = 0
    for batch in session.scalars(
        select(CardDeletionBatch).where(
            CardDeletionBatch.user_id == user_id, CardDeletionBatch.status == "PENDING"
        )
    ).all():
        if batch.undo_until > now:
            continue
        for card in _batch_cards(session, batch_id=batch.delete_batch_id):
            session.delete(card)  # FK 级联 review_states/review_events
        batch.status = "FINALIZED"
        batch.updated_at = now
        finalized += 1
    return finalized


def mark_card_deleted(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    delete_batch_id: str | None,
    now: str,
) -> dict[str, object]:
    """删除单卡（进入 10 秒撤销批次，不立即硬删；3.18/6.5 DELETE /cards/{card_id}）。

    delete_batch_id 提供且对应批仍 PENDING → 合并追加并整批重计时（D-24）；
    否则（缺失/过期/终态）→ 新建批。卡归属与可见性经 _owned_card（统一可见谓词）：
    STAGED/已在批内的卡 → 404 CARD_NOT_FOUND（4.1 不可见即不存在）。
    """
    finalize_expired_batches(session, user_id=user_id, now=now)
    card = _owned_card(session, user_id=user_id, card_id=card_id)
    undo_until = _now_plus_window(now)
    batch: CardDeletionBatch | None = None
    if delete_batch_id:
        batch = session.scalar(
            select(CardDeletionBatch).where(
                CardDeletionBatch.delete_batch_id == delete_batch_id,
                CardDeletionBatch.user_id == user_id,
                CardDeletionBatch.status == "PENDING",
            )
        )
    if batch is None:
        batch = CardDeletionBatch(
            delete_batch_id=str(uuid.uuid4()),
            user_id=user_id,
            status="PENDING",
            undo_until=undo_until,
            created_at=now,
            updated_at=now,
        )
        session.add(batch)
        session.flush()
    else:
        # 合并：原子刷新整批 undo_until = now + 10s，成员卡同步（3.18 追加重计时）
        batch.undo_until = undo_until
        batch.updated_at = now
        session.execute(
            update(Card)
            .where(Card.delete_batch_id == batch.delete_batch_id)
            .values(undo_until=undo_until)
        )
    card.delete_batch_id = batch.delete_batch_id
    card.pending_delete_at = now
    card.undo_until = undo_until
    card.updated_at = now
    session.flush()
    return _batch_view(session, batch)


def list_pending_batches(session: Session, *, user_id: str, now: str) -> list[dict[str, object]]:
    """App 重启恢复仍有效的撤销批次（6.5 GET pending；D-19）：先惰性清理过期批。"""
    finalize_expired_batches(session, user_id=user_id, now=now)
    batches = session.scalars(
        select(CardDeletionBatch)
        .where(CardDeletionBatch.user_id == user_id, CardDeletionBatch.status == "PENDING")
        .order_by(CardDeletionBatch.created_at, CardDeletionBatch.delete_batch_id)
    ).all()
    return [_batch_view(session, b) for b in batches]


def undo_deletion_batch(
    session: Session, *, user_id: str, delete_batch_id: str, now: str
) -> dict[str, object]:
    """撤销整批（6.5 POST undo）：窗口内同一事务清空所有卡片删除标记并置 UNDONE。

    过期/终态批 → 409 CARD_DELETE_WINDOW_EXPIRED（撤销窗口左闭右开：
    undo_until == now 即过期，超时最终删除后不提供恢复入口）；跨用户 → 404。
    """
    finalize_expired_batches(session, user_id=user_id, now=now)
    batch = session.scalar(
        select(CardDeletionBatch).where(
            CardDeletionBatch.delete_batch_id == delete_batch_id,
            CardDeletionBatch.user_id == user_id,
        )
    )
    if batch is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "删除批次不存在")
    if batch.status != "PENDING" or batch.undo_until <= now:
        raise AppError(ErrorCode.CARD_DELETE_WINDOW_EXPIRED, "撤销窗口已过")
    for card in _batch_cards(session, batch_id=batch.delete_batch_id):
        card.delete_batch_id = None
        card.pending_delete_at = None
        card.undo_until = None
        card.updated_at = now
    batch.status = "UNDONE"
    batch.updated_at = now
    session.flush()
    return _batch_view(session, batch)
