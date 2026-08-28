"""Task execution leases and fencing helpers.

The database row is the queue.  A lease is deliberately short lived and acquired with a
conditional UPDATE so two API processes cannot both own the same task.  ``lease_version`` is
incremented on every takeover; workers must carry both the token and version when writing results.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.task import ACTIVE_TASK_STATUSES
from infra.clock import SystemClock
from infra.db.models import Task
from infra.db.session import format_utc

DEFAULT_LEASE_SECONDS = 180


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    worker_id: str
    token: str
    version: int
    lease_until: str


def _lease_until(now: str, seconds: int) -> str:
    try:
        parsed = datetime.fromisoformat(now)
    except ValueError:
        parsed = SystemClock().now_utc()
    return format_utc(parsed + timedelta(seconds=max(1, seconds)))


def claim_task(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    now: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    statuses: frozenset[str] = ACTIVE_TASK_STATUSES,
    stages: frozenset[str] | None = None,
) -> TaskLease | None:
    """Atomically claim one active task, or return ``None`` when another worker owns it."""
    token = str(uuid.uuid4())
    until = _lease_until(now, lease_seconds)
    predicates: list[Any] = [
        Task.task_id == task_id,
        Task.status.in_(statuses),
        or_(Task.lease_until.is_(None), Task.lease_until <= now),
        or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now),
    ]
    if stages is not None:
        predicates.append(Task.stage.in_(stages))
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(*predicates)
            .values(
                claimed_by=worker_id,
                lease_token=token,
                lease_until=until,
                lease_version=Task.lease_version + 1,
                attempt_count=Task.attempt_count + 1,
                updated_at=now,
            )
        ),
    )
    if result.rowcount != 1:
        return None
    version = session.scalar(select(Task.lease_version).where(Task.task_id == task_id))
    if version is None:
        return None
    return TaskLease(task_id, worker_id, token, int(version), until)


def renew_task(
    session: Session,
    lease: TaskLease,
    *,
    now: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """Extend a lease only when the exact fencing token/version is still current."""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == lease.task_id,
                Task.claimed_by == lease.worker_id,
                Task.lease_token == lease.token,
                Task.lease_version == lease.version,
                Task.status.in_(ACTIVE_TASK_STATUSES),
            )
            .values(lease_until=_lease_until(now, lease_seconds), updated_at=now)
        ),
    )
    return result.rowcount == 1


def release_task(session: Session, lease: TaskLease, *, now: str) -> bool:
    """Clear a lease without changing the task state."""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == lease.task_id,
                Task.claimed_by == lease.worker_id,
                Task.lease_token == lease.token,
                Task.lease_version == lease.version,
            )
            .values(
                claimed_by=None,
                lease_token=None,
                lease_until=None,
                lease_version=Task.lease_version + 1,
                updated_at=now,
            )
        ),
    )
    return result.rowcount == 1


def require_lease(
    session: Session,
    *,
    task_id: str,
    worker_id: str,
    token: str,
    version: int,
    now: str | None = None,
) -> None:
    """Fail closed when a stale worker tries to write a task result."""
    current = session.execute(
        select(Task.lease_until, Task.status).where(
            Task.task_id == task_id,
            Task.claimed_by == worker_id,
            Task.lease_token == token,
            Task.lease_version == version,
        )
    ).one_or_none()
    if current is None:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务执行权已失效，请由新的执行者继续")
    lease_until, status = current
    if status not in ACTIVE_TASK_STATUSES:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务已不再处于可执行状态")
    if now is not None and lease_until is not None and lease_until <= now:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务执行租约已过期")


def recover_expired_leases(session: Session, *, now: str, stale_before: str | None = None) -> int:
    """Invalidate expired owners so the next claim receives a new fencing version.

    ``stale_before`` lets the scheduler reconcile a worker whose heartbeat has exceeded the
    business orphan window even when its short lease TTL has not elapsed (for example after a
    process was suspended while the clock advanced).  A fresh heartbeat always wins.
    """
    expiry = Task.lease_until.is_not(None) & (Task.lease_until < now)
    predicates: Any = expiry
    if stale_before is not None:
        # A heartbeat can be stale before the short TTL elapses.  Restrict this branch to rows
        # that actually have an owner; an unclaimed task with an old historical timestamp must
        # remain eligible for the normal first claim and must not have its heartbeat refreshed.
        predicates = or_(expiry, Task.lease_until.is_not(None) & (Task.updated_at < stale_before))
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(predicates)
            .values(
                claimed_by=None,
                lease_token=None,
                lease_until=None,
                lease_version=Task.lease_version + 1,
            )
        ),
    )
    return int(result.rowcount)
