"""services.cards：卡片用例（position 分配/创建/列表/导入原子/初始排程状态）。

卡片创建同事务插入初始 review_states（database-design §3：state=NEW、difficulty=1.0 满足
ORM CHECK 1~10）；position = 牌组内 max+1（UNIQUE(deck_id, position) 并发兜底）。
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from infra.db.models import Card, ReviewState
from services.decks.service import _owned


def _card_id() -> str:
    return str(uuid.uuid4())


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1


def _insert_card(
    session: Session, *, deck_id: str, device_id: str, front: str, back: str, now: str
) -> Card:
    card = Card(
        card_id=_card_id(),
        deck_id=deck_id,
        device_id=device_id,
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
    session: Session, *, device_id: str, deck_id: str, front: str, back: str, now: str
) -> Card:
    _owned(session, device_id=device_id, deck_id=deck_id)
    return _insert_card(
        session, deck_id=deck_id, device_id=device_id, front=front, back=back, now=now
    )


def list_cards(session: Session, *, device_id: str, deck_id: str) -> list[Card]:
    _owned(session, device_id=device_id, deck_id=deck_id)
    return list(
        session.scalars(select(Card).where(Card.deck_id == deck_id).order_by(Card.position)).all()
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
    device_id: str,
    deck_id: str,
    cards: Iterable[tuple[str, str]],
    now: str,
) -> list[dict[str, object]]:
    """原子导入：同事务逐张插入；任何写入失败整体回滚（调用方 rollback），不残留部分写入。"""
    _owned(session, device_id=device_id, deck_id=deck_id)
    results: list[dict[str, object]] = []
    for index, (front, back) in enumerate(cards):
        card = _insert_card(
            session, deck_id=deck_id, device_id=device_id, front=front, back=back, now=now
        )
        results.append({"index": index, "status": "CREATED", "card_id": card.card_id})
    return results
