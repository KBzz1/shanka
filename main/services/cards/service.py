"""services.cards：卡片用例（position 分配/创建/列表/导入原子/初始排程状态）。

卡片创建同事务插入初始 review_states（database-design §3：state=NEW、difficulty=1.0 满足
ORM CHECK 1~10）；position = 牌组内 max+1（UNIQUE(deck_id, position) 并发兜底）。

V2.5（3.9）：所有用户侧卡查询复用 domain/card.py 统一可见谓词
（publication_state='PUBLISHED' AND delete_batch_id IS NULL）——STAGED 卡在任务
整体成功前对任何用户侧查询不可见（4.1），禁止本模块自行拼等价条件。
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.db.models import Card, ReviewState
from services.decks.service import _owned


def _card_id() -> str:
    return str(uuid.uuid4())


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1


def _owned_card(session: Session, *, user_id: str, card_id: str) -> Card:
    """归属查卡（PATCH/DELETE 单卡用；跨用户统一 404，契约 1.1）。

    统一可见谓词（3.9）：STAGED/删除批次卡对用户单卡操作同样不可见（4.1：
    对任何用户侧查询不可见）→ 404 不暴露存在性。
    """
    card = session.scalar(
        select(Card).where(
            Card.card_id == card_id,
            Card.user_id == user_id,
            text(VISIBLE_PREDICATE_SQL),
        )
    )
    if card is None:
        raise AppError(ErrorCode.CARD_NOT_FOUND, "卡片不存在")
    return card


def update_card(
    session: Session, *, user_id: str, card_id: str, front: str, back: str, now: str
) -> Card:
    """编辑卡片（structure-contract 6.5；用户决策 2026-08-11：与重写同语义）。

    内容覆盖 + ReviewState 重置为新卡初始值（内容已变，旧记忆不适用）；
    version=now（与创建一致，天然递增；兼容 rewrite 的 v 数字转换逻辑）。
    """
    card = _owned_card(session, user_id=user_id, card_id=card_id)
    card.front = front
    card.back = back
    card.version = now
    card.updated_at = now
    rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
    if rs is not None:
        rs.state = "NEW"
        rs.stability = 0.0
        rs.difficulty = 1.0
        rs.due = now
        rs.reps = 0
        rs.lapses = 0
        rs.last_review = None
        rs.last_rating = None
        rs.updated_at = now
    return card


def delete_card(session: Session, *, user_id: str, card_id: str) -> None:
    """删除单卡（structure-contract 6.5）；review_states/review_events 由 FK ON DELETE
    CASCADE 级联清理（engine 级 PRAGMA foreign_keys=ON）。"""
    card = _owned_card(session, user_id=user_id, card_id=card_id)
    session.delete(card)


def _insert_card(
    session: Session, *, deck_id: str, user_id: str, front: str, back: str, now: str
) -> Card:
    card = Card(
        card_id=_card_id(),
        deck_id=deck_id,
        user_id=user_id,
        source="MANUAL",
        position=_next_position(session, deck_id=deck_id),
        front=front,
        back=back,
        card_type="QUESTION",
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(card)
    session.flush()  # 立即暴露 UNIQUE(deck_id, position) 冲突
    session.add(
        ReviewState(
            review_state_id=_card_id(),
            card_id=card.card_id,
            state="NEW",
            stability=0.0,
            difficulty=1.0,  # ORM CHECK 1~10（Task 2 已踩坑：0.0 违约）
            due=now,
            reps=0,
            lapses=0,
            updated_at=now,
        )
    )
    return card


def create_card(
    session: Session, *, user_id: str, deck_id: str, front: str, back: str, now: str
) -> Card:
    _owned(session, user_id=user_id, deck_id=deck_id)
    return _insert_card(session, deck_id=deck_id, user_id=user_id, front=front, back=back, now=now)


def list_cards(session: Session, *, user_id: str, deck_id: str) -> list[Card]:
    """牌组卡片列表（6.5）：只含可见卡（统一可见谓词 3.9）。"""
    _owned(session, user_id=user_id, deck_id=deck_id)
    return list(
        session.scalars(
            select(Card)
            .where(Card.deck_id == deck_id, text(VISIBLE_PREDICATE_SQL))
            .order_by(Card.position)
        ).all()
    )


def card_view(card: Card) -> dict[str, object]:
    return {
        "card_id": card.card_id,
        "deck_id": card.deck_id,
        "source": card.source,
        "position": card.position,
        "front": card.front,
        "back": card.back,
        "code": card.code,
        "card_type": card.card_type,
        "question": card.question,
        "answer": card.answer,
        "statement": card.statement,
        "answer_boolean": card.answer_boolean,
        "explanation": card.explanation,
        "generation_item_id": card.generation_item_id,
        "source_task_id": card.source_task_id,  # V2.5 生成来源任务；删历史保留卡时置空
        "chapter_id": card.chapter_id,  # V2.5 源章节；null 显示"未归属章节"
        "publication_state": card.publication_state,  # V2.5 STAGED/PUBLISHED
        "delete_batch_id": card.delete_batch_id,  # V2.5 非空 = 10 秒待删除批次
        "pending_delete_at": card.pending_delete_at,  # V2.5 服务端计时
        "undo_until": card.undo_until,  # V2.5 服务端撤销窗口
        "target_difficulty": card.target_difficulty,
        "knowledge_point_ids": card.knowledge_point_ids,
        "evidence_score": card.evidence_score,
        "correctness_score": card.correctness_score,
        "difficulty_score": card.difficulty_score,
        "learning_value_score": card.learning_value_score,
        "rubric_total_score": card.rubric_total_score,
        "version": card.version,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }


def import_cards(
    session: Session,
    *,
    user_id: str,
    deck_id: str,
    cards: Iterable[tuple[str, str]],
    now: str,
) -> list[dict[str, object]]:
    """原子导入：同事务逐张插入；任何写入失败整体回滚（调用方 rollback），不残留部分写入。"""
    _owned(session, user_id=user_id, deck_id=deck_id)
    results: list[dict[str, object]] = []
    for index, (front, back) in enumerate(cards):
        card = _insert_card(
            session, deck_id=deck_id, user_id=user_id, front=front, back=back, now=now
        )
        results.append({"index": index, "status": "CREATED", "card_id": card.card_id})
    return results
