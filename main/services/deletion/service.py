"""Deletion coordination for project/deck resources.

The preflight response is deliberately read-only and advisory.  The destructive operation calls
the same helpers again inside its write transaction, so a client cannot turn a stale preflight
into a partial delete.  SQLite's write transaction (claimed by ``execute_idempotent`` for HTTP
writes) serializes a competing task transition while the resource is being removed.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.task import ACTIVE_TASK_STATUSES
from infra.db.models import Batch, LlmCallAttempt, Task
from services.tasks.operations import finish_operation

PRE_GENERATION_TASK_STATUSES = frozenset(
    {"DRAFT", "SAMPLE_GENERATING", "AWAITING_SAMPLE_CONFIRMATION"}
)
NON_INTERRUPTIBLE_TASK_STATUSES = frozenset({"GENERATING"})
CANCELABLE_TASK_STATUSES = ACTIVE_TASK_STATUSES


def resource_tasks(
    session: Session,
    *,
    user_id: str,
    project_id: str | None = None,
    deck_id: str | None = None,
    file_id: str | None = None,
) -> list[Task]:
    """Return active tasks referencing exactly one owned resource, in stable order."""
    if sum(value is not None for value in (project_id, deck_id, file_id)) != 1:
        raise ValueError("exactly one deletion resource must be supplied")
    predicates = [Task.user_id == user_id, Task.status.in_(ACTIVE_TASK_STATUSES)]
    predicates.append(
        Task.project_id == project_id
        if project_id is not None
        else Task.deck_id == deck_id
        if deck_id is not None
        else Task.file_id == file_id
    )
    return list(
        session.scalars(
            select(Task).where(*predicates).order_by(Task.updated_at, Task.task_id)
        ).all()
    )


def task_blocker_view(task: Task) -> dict[str, object]:
    """Stable, non-sensitive task summary suitable for a deletion response."""
    can_abandon = task.status in PRE_GENERATION_TASK_STATUSES
    can_cancel = task.status in CANCELABLE_TASK_STATUSES
    return {
        "task_id": task.task_id,
        "status": task.status,
        "internal_stage": task.stage,
        "project_id": task.project_id,
        "deck_id": task.deck_id,
        "can_abandon": can_abandon,
        "can_cancel": can_cancel,
        "allowed_actions": ["ABANDON_AND_RETRY"]
        if can_abandon
        else ["WAIT_FOR_TERMINAL", "VIEW_TASKS"],
    }


def preflight_payload(
    session: Session,
    *,
    user_id: str,
    resource_type: str,
    resource_id: str,
    impact: dict[str, object],
    project_id: str | None = None,
    deck_id: str | None = None,
    allow_cancel: bool = False,
) -> dict[str, object]:
    """Build a read-only deletion preflight payload for a project or deck."""
    blockers = resource_tasks(session, user_id=user_id, project_id=project_id, deck_id=deck_id)
    blocker_views = [task_blocker_view(task) for task in blockers]
    abandonable = [task.task_id for task in blockers if task.status in PRE_GENERATION_TASK_STATUSES]
    has_uncancellable = any(task.status in NON_INTERRUPTIBLE_TASK_STATUSES for task in blockers)
    actions: list[str] = []
    if abandonable:
        actions.append("ABANDON_AND_RETRY")
    if has_uncancellable:
        actions.extend(["WAIT_FOR_TERMINAL", "VIEW_TASKS"])
    if allow_cancel and blockers:
        actions.insert(0, "CANCEL_AND_DELETE")
        for blocker in blocker_views:
            allowed_actions = blocker.get("allowed_actions")
            if (
                blocker.get("can_cancel") is True
                and isinstance(allowed_actions, list)
                and "CANCEL_AND_DELETE" not in allowed_actions
            ):
                blocker["allowed_actions"] = ["CANCEL_AND_DELETE", *allowed_actions]
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "can_delete": not blockers,
        "blockers": blocker_views,
        "abandonable_task_ids": abandonable,
        "has_uncancellable_tasks": has_uncancellable,
        "cancelable_task_ids": [
            task.task_id for task in blockers if task.status in CANCELABLE_TASK_STATUSES
        ],
        "can_cancel": bool(blockers)
        and all(task.status in CANCELABLE_TASK_STATUSES for task in blockers),
        "actions": list(dict.fromkeys(actions)),
        "impact": impact,
    }


def abandon_pre_generation(
    session: Session,
    *,
    user_id: str,
    tasks: list[Task],
    now: str,
    resource_type: str,
    resource_id: str,
    error_code: ErrorCode = ErrorCode.PROJECT_HAS_ACTIVE_TASK,
) -> list[str]:
    """CAS-abandon pre-generation blockers; reject a formal generation in progress.

    This function is called from the same transaction as the subsequent resource delete.  It
    rechecks every row with a conditional UPDATE instead of trusting a prior GET/preflight.
    """
    non_interruptible = [task for task in tasks if task.status in NON_INTERRUPTIBLE_TASK_STATUSES]
    if non_interruptible:
        raise AppError(
            error_code,
            "正式生成正在进行，请等待任务结束后再删除",
            actions=("WAIT_FOR_TERMINAL", "VIEW_TASKS"),
            details={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "task_ids": [task.task_id for task in non_interruptible],
            },
        )
    abandoned: list[str] = []
    for task in tasks:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(Task.task_id == task.task_id, Task.status.in_(PRE_GENERATION_TASK_STATUSES))
                .values(
                    status="ABANDONED",
                    stage=None,
                    ended_at=now,
                    resumable=0,
                    updated_at=now,
                    claimed_by=None,
                    lease_token=None,
                    lease_until=None,
                    lease_version=Task.lease_version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            # A competing transition won the CAS.  Fail closed; the outer transaction rolls back
            # all abandoned rows and the caller can refresh the preflight.
            raise AppError(
                error_code,
                "任务状态刚刚变化，请刷新后重试",
                actions=("VIEW_TASKS", "WAIT_FOR_TERMINAL"),
                details={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "task_ids": [task.task_id],
                },
            )
        abandoned.append(task.task_id)
        session.execute(
            update(LlmCallAttempt)
            .where(
                LlmCallAttempt.task_id == task.task_id,
                LlmCallAttempt.status == "STARTED",
            )
            .values(status="UNKNOWN", finished_at=now)
        )
        finish_operation(
            session,
            task_id=task.task_id,
            status="ABANDONED",
            now=now,
            reason="USER_ABANDON",
        )
    remaining = resource_tasks(
        session,
        user_id=user_id,
        project_id=resource_id if resource_type == "PROJECT" else None,
        deck_id=resource_id if resource_type == "DECK" else None,
    )
    if remaining:
        raise AppError(
            error_code,
            "仍有进行中的任务，请刷新后重试",
            actions=("VIEW_TASKS", "WAIT_FOR_TERMINAL"),
            details={"resource_type": resource_type, "resource_id": resource_id},
        )
    return abandoned


def cancel_active_tasks(
    session: Session,
    *,
    user_id: str,
    tasks: list[Task],
    now: str,
    resource_type: str,
    resource_id: str,
    file_id: str | None = None,
    error_code: ErrorCode = ErrorCode.PROJECT_HAS_ACTIVE_TASK,
) -> list[str]:
    """Cancel every active generation task for a resource as one destructive decision.

    Unlike ``abandon_pre_generation`` this deliberately includes ``GENERATING``. It first fences
    workers (status CAS + lease clear), marks in-flight ledger calls UNKNOWN, and resets
    PROCESSING batches. The caller can then delete the resource and task rows in the same write
    transaction; a stale worker can neither publish nor revive the canceled task.
    """
    canceled: list[str] = []
    for task in tasks:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(Task.task_id == task.task_id, Task.user_id == user_id)
                .where(Task.status.in_(CANCELABLE_TASK_STATUSES))
                .values(
                    status="ABANDONED",
                    stage=None,
                    ended_at=now,
                    resumable=0,
                    updated_at=now,
                    claimed_by=None,
                    lease_token=None,
                    lease_until=None,
                    lease_version=Task.lease_version + 1,
                )
            ),
        )
        if result.rowcount != 1:
            raise AppError(
                error_code,
                "任务状态刚刚变化，请刷新后重试",
                actions=("VIEW_TASKS", "WAIT_FOR_TERMINAL"),
                details={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "task_ids": [task.task_id],
                },
            )
        session.execute(
            update(LlmCallAttempt)
            .where(
                LlmCallAttempt.task_id == task.task_id,
                LlmCallAttempt.status == "STARTED",
            )
            .values(status="UNKNOWN", finished_at=now)
        )
        session.execute(
            update(Batch)
            .where(Batch.task_id == task.task_id, Batch.status == "PROCESSING")
            .values(status="FAILED")
        )
        finish_operation(
            session,
            task_id=task.task_id,
            status="ABANDONED",
            now=now,
            reason="RESOURCE_DELETE",
        )
        canceled.append(task.task_id)
    remaining = resource_tasks(
        session,
        user_id=user_id,
        project_id=resource_id if resource_type == "PROJECT" else None,
        deck_id=resource_id if resource_type == "DECK" else None,
        file_id=file_id if resource_type == "PDF" else None,
    )
    if remaining:
        raise AppError(
            error_code,
            "仍有进行中的任务，请刷新后重试",
            actions=("VIEW_TASKS", "WAIT_FOR_TERMINAL"),
            details={"resource_type": resource_type, "resource_id": resource_id},
        )
    return canceled
