"""services.decks：牌组用例（创建/列表/详情/删除/进度聚合/删除保护）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
幂等记录与业务副作用同事务由 handler 层 execute_idempotent 包装（Task 4）。
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Card, Deck, ReviewEvent, ReviewState, Task


def _deck_id() -> str:
    return str(uuid.uuid4())


def create_deck(session: Session, *, user_id: str, name: str, now: str) -> Deck:
    deck = Deck(
        deck_id=_deck_id(),
        user_id=user_id,
        name=name,
        source="MANUAL",
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(deck)
    return deck


def rename_deck(session: Session, *, user_id: str, deck_id: str, name: str, now: str) -> Deck:
    """牌组改名（structure-contract 6.5）：version 递增供客户端缓存刷新。"""
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    deck.name = name
    deck.version = now
    deck.updated_at = now
    return deck


def _owned(session: Session, *, user_id: str, deck_id: str) -> Deck:
    deck = session.get(Deck, deck_id)
    if deck is None or deck.user_id != user_id:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在")
    return deck


def deck_progress(
    session: Session, *, user_id: str, deck_id: str, now: str
) -> dict[str, int | float]:
    """派生进度（structure-contract 3.8/5.3）：card_count/due_count/mastered/review_count/mastery_ratio。"""
    _owned(session, user_id=user_id, deck_id=deck_id)
    card_count = (
        session.scalar(select(func.count(Card.card_id)).where(Card.deck_id == deck_id)) or 0
    )
    due_count = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(Card.deck_id == deck_id, ReviewState.due <= now)
        )
        or 0
    )
    mastered = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.deck_id == deck_id,
                ReviewState.state == "REVIEW",
                ReviewState.stability >= 21,
            )
        )
        or 0
    )
    review_count = (
        session.scalar(
            select(func.count(ReviewEvent.review_event_id))
            .join(Card, Card.card_id == ReviewEvent.card_id)
            .where(Card.deck_id == deck_id)
        )
        or 0
    )
    return {
        "card_count": card_count,
        "due_count": due_count,
        "mastered_card_count": mastered,
        "review_count": review_count,
        "mastery_ratio": float(mastered) / card_count if card_count else 0.0,
    }


def _to_deck_view(deck: Deck, progress: dict[str, int | float]) -> dict[str, object]:
    return {
        "deck_id": deck.deck_id,
        "name": deck.name,
        "source": deck.source,
        "card_count": progress["card_count"],
        "due_count": progress["due_count"],
        "mastered_card_count": progress["mastered_card_count"],
        "review_count": progress["review_count"],
        "mastery_ratio": progress["mastery_ratio"],
        "created_at": deck.created_at,
        "updated_at": deck.updated_at,
        "version": deck.version,
    }


def list_decks(session: Session, *, user_id: str, now: str) -> list[dict[str, object]]:
    decks = session.scalars(
        select(Deck).where(Deck.user_id == user_id).order_by(Deck.updated_at.desc())
    ).all()
    result: list[dict[str, object]] = []
    for deck in decks:
        result.append(
            _to_deck_view(
                deck, deck_progress(session, user_id=user_id, deck_id=deck.deck_id, now=now)
            )
        )
    return result


def get_deck(session: Session, *, user_id: str, deck_id: str, now: str) -> dict[str, object]:
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    return _to_deck_view(deck, deck_progress(session, user_id=user_id, deck_id=deck_id, now=now))


def delete_deck(session: Session, *, user_id: str, deck_id: str) -> None:
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    blocking = (
        session.scalar(
            select(func.count(Task.task_id)).where(
                Task.deck_id == deck_id,
                Task.user_id == user_id,  # 一致性守卫（DESIGN §5.1）：只计本用户任务
                Task.status.in_(["PENDING", "RUNNING", "PAUSED"]),
            )
        )
        or 0
    )
    if blocking:
        raise AppError(ErrorCode.TASK_IN_PROGRESS, "存在进行中的任务引用该牌组")
    # tasks.deck_id SET NULL（database-design §3）；cards 级联由 FK ON DELETE CASCADE 处理
    for task in session.scalars(
        select(Task).where(Task.deck_id == deck_id, Task.user_id == user_id)
    ).all():
        task.deck_id = None
    session.delete(deck)
