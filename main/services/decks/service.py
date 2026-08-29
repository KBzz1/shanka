"""services.decks：牌组用例（创建/列表/详情/删除/进度聚合/删除保护）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
幂等记录与业务副作用同事务由 handler 层 execute_idempotent 包装（Task 4）。
"""

import uuid

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from infra.clock import SystemClock
from infra.db.models import (
    Card,
    Deck,
    GenerationOperation,
    LearningProject,
    LlmCallAttempt,
    ReviewEvent,
    ReviewState,
    Task,
)
from infra.db.session import format_utc
from services.deletion.service import (
    cancel_active_tasks as cancel_active_generation_tasks,
)
from services.deletion.service import (
    preflight_payload,
    resource_tasks,
)


def _deck_id() -> str:
    return str(uuid.uuid4())


def _owned_project(session: Session, *, user_id: str, project_id: str) -> LearningProject:
    """项目归属校验（与 projects.service/tasks.service 同口径）：不存在/跨用户 →
    404 PROJECT_NOT_FOUND 不暴露存在性。"""
    project = session.get(LearningProject, project_id)
    if project is None or project.user_id != user_id:
        raise AppError(ErrorCode.PROJECT_NOT_FOUND, "项目不存在或无权访问")
    return project


def create_deck(
    session: Session, *, user_id: str, name: str, now: str, project_id: str | None = None
) -> Deck:
    """创建牌组（structure-contract 3.8）：project_id 归属学习项目（null = 手动/独立）。

    V2.5（OPEN-1 裁决）：project_id 经归属校验后才落库——项目不存在/跨用户 →
    404 PROJECT_NOT_FOUND，不做静默 null 回落（否则 API 建牌组无法挂任务）。
    """
    if project_id is not None:
        _owned_project(session, user_id=user_id, project_id=project_id)
    deck = Deck(
        deck_id=_deck_id(),
        user_id=user_id,
        name=name,
        source="MANUAL",
        project_id=project_id,
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


def attach_deck_to_project(
    session: Session, *, user_id: str, project_id: str, deck_id: str, now: str
) -> Deck:
    """将独立牌组归入项目；归属后才可被项目今日计划选择。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    if deck.project_id is not None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "只有独立牌组可以归入项目")
    visible_count = session.scalar(
        select(func.count(Card.card_id)).where(
            Card.user_id == user_id,
            Card.deck_id == deck_id,
            text(VISIBLE_PREDICATE_SQL),
        )
    ) or 0
    if visible_count == 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, "卡组暂无可学习卡片")
    deck.project_id = project.project_id
    deck.updated_at = now
    deck.version = now
    session.flush()
    return deck


def _owned(session: Session, *, user_id: str, deck_id: str) -> Deck:
    deck = session.get(Deck, deck_id)
    if deck is None or deck.user_id != user_id:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在")
    return deck


def deck_progress(
    session: Session, *, user_id: str, deck_id: str, now: str
) -> dict[str, object]:
    """派生进度（structure-contract 3.8/5.3）：card_count/due_count/mastered/review_count/
    mastery_ratio——全部只含可见卡（统一可见谓词 3.9：card_count 派生进度只含可见卡）。"""
    _owned(session, user_id=user_id, deck_id=deck_id)
    visible = text(VISIBLE_PREDICATE_SQL)
    card_count = session.scalar(
        select(func.count(Card.card_id)).where(Card.user_id == user_id, Card.deck_id == deck_id, visible)
    ) or 0
    due_count = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id == deck_id,
                ReviewState.state != "NEW",
                ReviewState.due <= now,
                visible,
            )
        )
        or 0
    )
    mastered = (
        session.scalar(
            select(func.count(Card.card_id))
            .join(ReviewState, ReviewState.card_id == Card.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id == deck_id,
                ReviewState.state == "REVIEW",
                ReviewState.stability >= 21,
                visible,
            )
        )
        or 0
    )
    review_count = (
        session.scalar(
            select(func.count(ReviewEvent.review_event_id))
            .join(Card, Card.card_id == ReviewEvent.card_id)
            .where(
                Card.user_id == user_id,
                Card.deck_id == deck_id,
                ReviewEvent.user_id == user_id,
                visible,
            )
        )
        or 0
    )
    lifecycle_rows = session.execute(
        select(ReviewState.state, ReviewState.stability)
        .select_from(Card)
        .outerjoin(ReviewState, ReviewState.card_id == Card.card_id)
        .where(Card.user_id == user_id, Card.deck_id == deck_id, visible)
    ).all()
    lifecycle = {"NEW": 0, "LEARNING": 0, "RELEARNING": 0, "CONSOLIDATING": 0, "MASTERED": 0}
    for state, stability in lifecycle_rows:
        if state is None or state == "NEW":
            lifecycle["NEW"] += 1
        elif state == "LEARNING":
            lifecycle["LEARNING"] += 1
        elif state == "RELEARNING":
            lifecycle["RELEARNING"] += 1
        elif state == "REVIEW" and float(stability or 0) >= 21:
            lifecycle["MASTERED"] += 1
        else:
            lifecycle["CONSOLIDATING"] += 1
    last_studied = session.scalar(
        select(func.max(ReviewEvent.reviewed_at))
        .join(Card, Card.card_id == ReviewEvent.card_id)
        .where(
            Card.user_id == user_id,
            Card.deck_id == deck_id,
            ReviewEvent.user_id == user_id,
            visible,
        )
    )
    return {
        "card_count": card_count,
        "due_count": due_count,
        "mastered_card_count": mastered,
        "review_count": review_count,
        "mastery_ratio": float(mastered) / card_count if card_count else 0.0,
        "not_started_count": lifecycle["NEW"],
        "learning_count": lifecycle["LEARNING"],
        "relearning_count": lifecycle["RELEARNING"],
        "consolidating_count": lifecycle["CONSOLIDATING"],
        "mastered_count": lifecycle["MASTERED"],
        "review_event_count": review_count,
        "last_studied_at": last_studied,
    }


def _to_deck_view(deck: Deck, progress: dict[str, object]) -> dict[str, object]:
    return {
        "deck_id": deck.deck_id,
        "name": deck.name,
        "source": deck.source,
        "project_id": deck.project_id,  # V2.5 归属学习项目；null = 手动/独立牌组
        "card_count": progress["card_count"],
        "due_count": progress["due_count"],
        "mastered_card_count": progress["mastered_card_count"],
        "review_count": progress["review_count"],
        "mastery_ratio": progress["mastery_ratio"],
        "not_started_count": progress["not_started_count"],
        "learning_count": progress["learning_count"],
        "relearning_count": progress["relearning_count"],
        "consolidating_count": progress["consolidating_count"],
        "mastered_count": progress["mastered_count"],
        "review_event_count": progress["review_event_count"],
        "last_studied_at": progress["last_studied_at"],
        "created_at": deck.created_at,
        "updated_at": deck.updated_at,
        "version": deck.version,
    }


def list_decks(
    session: Session,
    *,
    user_id: str,
    now: str,
    project_id: str | None = None,
) -> list[dict[str, object]]:
    """牌组列表（openapi listDecks）：user 域 + 可选 project_id 归属过滤。

    过滤语义与 tasks 列表同口径：project 不存在/跨用户 → 404 PROJECT_NOT_FOUND；
    归属项目无牌组 → 200 空列表。
    """
    stmt = select(Deck).where(Deck.user_id == user_id)
    if project_id is not None:
        _owned_project(session, user_id=user_id, project_id=project_id)
        stmt = stmt.where(Deck.project_id == project_id)
    decks = session.scalars(stmt.order_by(Deck.updated_at.desc())).all()
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


def delete_deck(
    session: Session,
    *,
    user_id: str,
    deck_id: str,
    abandon_pre_generation_tasks: bool = False,
    cancel_active_tasks: bool = False,
    now: str | None = None,
) -> None:
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    active_tasks = resource_tasks(session, user_id=user_id, deck_id=deck_id)
    if active_tasks:
        cancel_active_generation_tasks(
            session,
            user_id=user_id,
            tasks=active_tasks,
            now=now or format_utc(SystemClock().now_utc()),
            resource_type="DECK",
            resource_id=deck_id,
            error_code=ErrorCode.TASK_IN_PROGRESS,
        )
    # tasks.deck_id SET NULL（database-design §3）；cards 级联由 FK ON DELETE CASCADE 处理。
    # 活跃任务已在上方同一事务内自动取消，客户端无需传入任务处理选项。
    deck_tasks = session.scalars(
        select(Task).where(Task.deck_id == deck_id, Task.user_id == user_id)
    ).all()
    for task in deck_tasks:
        session.execute(
            update(LlmCallAttempt)
            .where(LlmCallAttempt.task_id == task.task_id)
            .values(task_id=None)
        )
        for operation in session.scalars(
            select(GenerationOperation).where(GenerationOperation.task_id == task.task_id)
        ).all():
            if operation.status == "ACTIVE":
                operation.status = "ABANDONED"
                operation.ended_at = now or format_utc(SystemClock().now_utc())
                operation.terminal_reason = "RESOURCE_DELETE"
            operation.task_id = None
            operation.updated_at = now or format_utc(SystemClock().now_utc())
        task.deck_id = None
    session.delete(deck)


def deck_deletion_preflight(
    session: Session,
    *,
    user_id: str,
    deck_id: str,
    allow_cancel: bool = False,
) -> dict[str, object]:
    """Read-only deck deletion preview; DELETE repeats it inside its write transaction."""
    deck = _owned(session, user_id=user_id, deck_id=deck_id)
    payload = preflight_payload(
        session,
        user_id=user_id,
        resource_type="DECK",
        resource_id=deck_id,
        deck_id=deck_id,
        allow_cancel=allow_cancel,
        impact={
            "deck_count": 1,
            "card_count": session.scalar(
                select(func.count(Card.card_id)).where(
                    Card.user_id == user_id,
                    Card.deck_id == deck_id,
                )
            )
            or 0,
            "task_count": session.scalar(
                select(func.count(Task.task_id)).where(
                    Task.deck_id == deck_id, Task.user_id == user_id
                )
            )
            or 0,
            "deck_name": deck.name,
        },
    )
    return payload
