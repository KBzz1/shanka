"""数据库租约、生成操作唯一性与删除围栏回归测试。

这些测试不调用 LLM：它们只验证队列抢占、幂等绑定和 destructive delete 的事务边界。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Batch, GenerationOperation, LlmCallAttempt, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from services.deletion.service import cancel_active_tasks, resource_tasks
from services.tasks.lease import claim_task, release_task, require_lease
from services.tasks.operations import begin_operation, bind_operation_task

_NOW = "2026-08-29T00:00:00.000Z"
_LATER = "2026-08-29T00:04:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'reliability.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as current:
        yield current


def _seed_user(session: Session) -> None:
    session.add(
        User(
            user_id="u1",
            username="u1",
            email="u1@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()


def _seed_task(
    session: Session,
    *,
    task_id: str = "task-1",
    user_id: str = "u1",
    file_id: str | None = None,
    status: str = "GENERATING",
    stage: str | None = "GENERATING",
) -> Task:
    task = Task(
        task_id=task_id,
        user_id=user_id,
        file_id=file_id,
        status=status,
        stage=stage,
        selected_chapters="[]",
        generation_config="{}",
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(task)
    session.flush()
    return task


def test_task_lease_is_exclusive_and_fenced(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        _seed_user(session)
        _seed_task(session)
        session.commit()

    with session_factory() as first:
        first_lease = claim_task(
            first,
            task_id="task-1",
            worker_id="worker-a",
            now=_NOW,
            lease_seconds=1,
        )
        assert first_lease is not None
        first.commit()

    with session_factory() as competing:
        assert (
            claim_task(
                competing,
                task_id="task-1",
                worker_id="worker-b",
                now=_NOW,
            )
            is None
        )
        competing.commit()

    with session_factory() as resumed:
        second_lease = claim_task(
            resumed,
            task_id="task-1",
            worker_id="worker-b",
            now=_LATER,
        )
        assert second_lease is not None
        assert second_lease.version > first_lease.version
        resumed.commit()

    with session_factory() as stale:
        with pytest.raises(AppError) as excinfo:
            require_lease(
                stale,
                task_id="task-1",
                worker_id=first_lease.worker_id,
                token=first_lease.token,
                version=first_lease.version,
                now=_LATER,
            )
        assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT


def test_release_makes_task_claimable_and_future_attempt_is_delayed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        _seed_user(session)
        task = _seed_task(session)
        task.next_attempt_at = _LATER
        session.commit()

    with session_factory() as first:
        assert claim_task(first, task_id="task-1", worker_id="worker-a", now=_NOW) is None
        first.commit()

    with session_factory() as first:
        first_lease = claim_task(first, task_id="task-1", worker_id="worker-a", now=_LATER)
        assert first_lease is not None
        assert release_task(first, first_lease, now=_LATER)
        first.commit()

    with session_factory() as resumed:
        assert claim_task(resumed, task_id="task-1", worker_id="worker-b", now=_LATER) is not None


def test_generation_operation_reuses_same_key_but_not_same_input(session: Session) -> None:
    _seed_user(session)
    operation, existing_task, deduplicated = begin_operation(
        session,
        user_id="u1",
        operation_key="create:stable-key",
        input_fingerprint="fp-a",
        now=_NOW,
    )
    assert existing_task is None
    assert deduplicated is False
    task = _seed_task(session, status="DRAFT", stage=None)
    bind_operation_task(session, operation, task, now=_NOW)
    session.commit()

    reused, reused_task, deduplicated = begin_operation(
        session,
        user_id="u1",
        operation_key="create:stable-key",
        input_fingerprint="fp-a",
        now=_LATER,
    )
    assert reused.operation_id == operation.operation_id
    assert reused_task is not None and reused_task.task_id == task.task_id
    assert deduplicated is True

    with pytest.raises(AppError) as excinfo:
        begin_operation(
            session,
            user_id="u1",
            operation_key="create:stable-key",
            input_fingerprint="fp-b",
            now=_LATER,
        )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    other, other_task, deduplicated = begin_operation(
        session,
        user_id="u1",
        operation_key="create:another-key",
        input_fingerprint="fp-a",
        now=_LATER,
    )
    assert other.operation_id != operation.operation_id
    assert other_task is None
    assert deduplicated is False


def test_cancel_active_generation_fences_worker_and_marks_side_effects(session: Session) -> None:
    _seed_user(session)
    pdf = PdfFile(
        file_id="pdf-1",
        user_id="u1",
        filename="source.pdf",
        storage_key="storage-1",
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    operation = GenerationOperation(
        operation_id="op-1",
        user_id="u1",
        operation_key="create:delete-me",
        input_fingerprint="fp-delete",
        status="ACTIVE",
        created_at=_NOW,
        updated_at=_NOW,
        task_id="task-1",
    )
    session.add(operation)
    task = _seed_task(session, file_id=pdf.file_id)
    task.operation_id = operation.operation_id
    task.claimed_by = "worker-a"
    task.lease_token = "lease-a"
    task.lease_version = 3
    task.lease_until = _LATER
    session.add(
        Batch(
            batch_id="batch-1",
            task_id=task.task_id,
            batch_index=0,
            status="PROCESSING",
            generated_item_ids="[]",
            created_at=_NOW,
        )
    )
    session.add(
        LlmCallAttempt(
            call_id="call-1",
            user_id="u1",
            scope_type="TASK",
            scope_id=task.task_id,
            task_id=task.task_id,
            operation_id=operation.operation_id,
            stage="GENERATING",
            operation_key="batch:0",
            attempt_no=1,
            input_fingerprint="fp-delete",
            model="test-model",
            prompt_name="test",
            prompt_version="v1",
            status="STARTED",
            created_at=_NOW,
        )
    )
    session.commit()

    tasks = resource_tasks(session, user_id="u1", file_id=pdf.file_id)
    assert [item.task_id for item in tasks] == [task.task_id]
    canceled = cancel_active_tasks(
        session,
        user_id="u1",
        tasks=tasks,
        now=_LATER,
        resource_type="PDF",
        resource_id=pdf.file_id,
        file_id=pdf.file_id,
    )
    assert canceled == [task.task_id]
    session.commit()

    refreshed = session.get(Task, task.task_id)
    assert refreshed is not None
    assert refreshed.status == "ABANDONED"
    assert refreshed.stage is None
    assert refreshed.lease_token is None
    assert session.scalar(select(Batch.status).where(Batch.batch_id == "batch-1")) == "FAILED"
    assert (
        session.scalar(select(LlmCallAttempt.status).where(LlmCallAttempt.call_id == "call-1"))
        == "UNKNOWN"
    )
    assert (
        session.scalar(
            select(GenerationOperation.status).where(GenerationOperation.operation_id == "op-1")
        )
        == "ABANDONED"
    )
