"""Persistent generation-operation identity and lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import GenerationOperation, Task


def normalized_input_fingerprint(
    *,
    user_id: str,
    project_id: str,
    deck_id: str,
    chapter_snapshot: list[dict[str, object]],
    generation_config: dict[str, object],
    behavior_version: str = "generation-operation-v1",
) -> str:
    payload = {
        "user_id": user_id,
        "project_id": project_id,
        "deck_id": deck_id,
        "chapters": chapter_snapshot,
        "generation_config": generation_config,
        "behavior_version": behavior_version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def begin_operation(
    session: Session,
    *,
    user_id: str,
    operation_key: str,
    input_fingerprint: str,
    now: str,
) -> tuple[GenerationOperation, Task | None, bool]:
    """Return ``(operation, existing_task, deduplicated)`` for a create intent.

    Nested savepoints handle races without rolling back the caller's idempotency transaction.
    """
    existing = session.scalar(
        select(GenerationOperation).where(
            GenerationOperation.user_id == user_id,
            GenerationOperation.operation_key == operation_key,
        )
    )
    if existing is not None:
        if existing.input_fingerprint != input_fingerprint:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "生成操作 key 与请求内容不一致")
        if existing.task_id is None and existing.status != "ACTIVE":
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "该生成操作已结束，不能复用操作 key")
        task = session.get(Task, existing.task_id) if existing.task_id else None
        return existing, task, True

    operation = GenerationOperation(
        operation_id=str(uuid.uuid4()),
        user_id=user_id,
        operation_key=operation_key,
        input_fingerprint=input_fingerprint,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(operation)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(GenerationOperation).where(
                GenerationOperation.user_id == user_id,
                GenerationOperation.operation_key == operation_key,
            )
        )
        if existing is None:
            raise
        if existing.input_fingerprint != input_fingerprint:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "生成操作 key 与请求内容不一致")
        if existing.task_id is None and existing.status != "ACTIVE":
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "该生成操作已结束，不能复用操作 key")
        task = session.get(Task, existing.task_id) if existing.task_id else None
        return existing, task, True
    return operation, None, False


def bind_operation_task(
    session: Session, operation: GenerationOperation, task: Task, *, now: str
) -> None:
    operation.task_id = task.task_id
    operation.updated_at = now
    task.operation_id = operation.operation_id


def finish_operation(
    session: Session,
    *,
    task_id: str,
    status: str,
    now: str,
    reason: str | None = None,
) -> None:
    operation = session.scalar(
        select(GenerationOperation).where(GenerationOperation.task_id == task_id)
    )
    if operation is None:
        return
    operation.status = status
    operation.terminal_reason = reason
    operation.updated_at = now
    operation.ended_at = now if status != "ACTIVE" else None
