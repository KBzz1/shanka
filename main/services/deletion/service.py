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
from infra.db.models import Task

PRE_GENERATION_TASK_STATUSES = frozenset(
    {"DRAFT", "SAMPLE_GENERATING", "AWAITING_SAMPLE_CONFIRMATION"}
)
NON_INTERRUPTIBLE_TASK_STATUSES = frozenset({"GENERATING"})


def resource_tasks(
    session: Session,
    *,
    user_id: str,
    project_id: str | None = None,
    deck_id: str | None = None,
) -> list[Task]:
    """Return active tasks referencing exactly one owned resource, in stable order."""
    if (project_id is None) == (deck_id is None):
        raise ValueError("exactly one deletion resource must be supplied")
    predicates = [Task.user_id == user_id, Task.status.in_(ACTIVE_TASK_STATUSES)]
    predicates.append(
        Task.project_id == project_id if project_id is not None else Task.deck_id == deck_id
    )
    return list(
        session.scalars(
            select(Task).where(*predicates).order_by(Task.updated_at, Task.task_id)
        ).all()
    )


def task_blocker_view(task: Task) -> dict[str, object]:
    """Stable, non-sensitive task summary suitable for a deletion response."""
    can_abandon = task.status in PRE_GENERATION_TASK_STATUSES
    return {
        "task_id": task.task_id,
        "status": task.status,
        "internal_stage": task.stage,
        "project_id": task.project_id,
        "deck_id": task.deck_id,
        "can_abandon": can_abandon,
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
    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "can_delete": not blockers,
        "blockers": blocker_views,
        "abandonable_task_ids": abandonable,
        "has_uncancellable_tasks": has_uncancellable,
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
                    ended_at=now,
                    resumable=0,
                    updated_at=now,
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
