"""llm_call_attempts 账本 service 测试（spec §9；Task 7）。

- 基座同 tests/services/pdf/conftest.py / test_tasks_service.py：真实 SQLite +
  create_db_engine（PRAGMA foreign_keys=ON）全表建库；
- llm_call_attempts.user_id/task_id 均 FK → 先补种 User + Task 行；
- brief 中 `settings_override` fixture 在仓库不存在且账本不消费 Settings，按
  仓库约定改用本文件局部 session fixture（adaptation 见任务报告）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Device, Task, User
from infra.db.session import create_db_engine, create_session_factory
from services.generation.ledger import (
    attempt_count,
    create_attempt,
    find_success_result,
    finish_failed,
    finish_success,
    mark_stale_unknown,
    scoring_attempt_total,
    task_token_totals,
)

_NOW = "2026-08-10T09:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def _seed(session: Session) -> None:
    """FK 前置：users + devices + tasks（task_id="t1" 与 brief 测试常量一致）。

    Task 保持 device 域种子（仅作 task_id 外键父行）；账本行归属切 user（P4-3）。
    """
    session.add(
        User(user_id="u1", username="u-1", password_hash="x", created_at=_NOW, updated_at=_NOW)
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    session.add(Device(device_id="d1", created_at=_NOW))
    session.add(
        Task(
            task_id="t1",
            device_id="d1",
            status="PENDING",
            selected_chapters="[]",
            generation_config="{}",
        )
    )
    session.flush()


def test_create_then_finish_success_roundtrip(session: Session) -> None:
    _seed(session)
    att = create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="fp1",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
        schema_name="planning_v3",
        schema_version="v3",
        rubric_version="v2",
        now=_NOW,
    )
    assert att.status == "STARTED"
    assert att.created_at == _NOW
    finish_success(
        session,
        att,
        usage={
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 5,
            "completion_tokens": 7,
        },
        http_status=200,
        duration_ms=10,
        normalized_result='{"units": []}',
        now=_NOW,
    )
    session.commit()
    assert (
        find_success_result(
            session,
            task_id="t1",
            stage="PLANNING",
            operation_key="planning:ch1:g0",
            input_fingerprint="fp1",
        )
        == '{"units": []}'
    )
    assert (
        attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1
    )
    # 资产版本与 usage 映射落库
    assert att.schema_name == "planning_v3"
    assert att.schema_version == "v3"
    assert att.rubric_version == "v2"
    assert att.cache_hit == 10
    assert att.cache_miss == 5
    assert att.output_tokens == 7
    assert att.http_status == 200
    assert att.duration_ms == 10
    assert att.finished_at == _NOW


def test_started_counts_toward_budget(session: Session) -> None:
    _seed(session)
    create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="fp2",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    session.commit()
    assert (
        attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1
    )


def test_mark_stale_unknown(session: Session) -> None:
    _seed(session)
    create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="fp3",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    session.commit()
    assert mark_stale_unknown(session, task_id="t1", stage="PLANNING", now=_NOW) == 1
    assert (
        mark_stale_unknown(session, task_id="t1", stage="PLANNING", now=_NOW) == 0
    )  # 已转 UNKNOWN
    # UNKNOWN 仍计入预算
    assert (
        attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1
    )


def test_duplicate_attempt_no_raises(session: Session) -> None:
    _seed(session)
    kw: dict[str, str] = {
        "user_id": "u1",
        "scope_type": "TASK",
        "scope_id": "t1",
        "task_id": "t1",
        "stage": "PLANNING",
        "operation_key": "k",
        "input_fingerprint": "f",
        "model": "m",
        "prompt_name": "p",
        "prompt_version": "v",
    }
    create_attempt(session, attempt_no=1, **kw)
    session.commit()  # spec §9：调用前先提交 STARTED 占位；冲突回滚不波及已提交占位
    with pytest.raises(AppError) as excinfo:
        create_attempt(session, attempt_no=1, **kw)  # 唯一约束冲突 → 409 语义
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    # 同操作不同 attempt_no 合法
    create_attempt(session, attempt_no=2, **kw)
    session.commit()


def test_finish_failed_records_error(session: Session) -> None:
    _seed(session)
    att = create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="fp4",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    finish_failed(session, att, error_code="API_KEY_UNAVAILABLE", now=_NOW)
    session.commit()
    assert att.status == "FAILED"
    assert att.error_code == "API_KEY_UNAVAILABLE"
    assert att.finished_at == _NOW
    assert (
        attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1
    )


def test_find_success_result_none_without_success(session: Session) -> None:
    _seed(session)
    create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="fp5",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    session.commit()
    assert (
        find_success_result(
            session,
            task_id="t1",
            stage="PLANNING",
            operation_key="planning:ch1:g0",
            input_fingerprint="fp5",
        )
        is None
    )


def test_scoring_attempt_total_counts_all_attempts(session: Session) -> None:
    _seed(session)
    for i in (1, 2, 3):
        create_attempt(
            session,
            user_id="u1",
            scope_type="TASK",
            scope_id="t1",
            task_id="t1",
            stage="SCORING",
            operation_key=f"scoring:g{i}",
            input_fingerprint=f"sf{i}",
            attempt_no=1,
            model="m",
            prompt_name="scoring",
            prompt_version="v2",
        )
    create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="p1",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    session.commit()
    assert scoring_attempt_total(session, task_id="t1") == 3


def test_task_token_totals_sums_per_stage(session: Session) -> None:
    _seed(session)
    p1 = create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="f1",
        attempt_no=1,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    finish_success(
        session,
        p1,
        usage={
            "prompt_cache_hit_tokens": 10,
            "prompt_cache_miss_tokens": 5,
            "completion_tokens": 7,
        },
        http_status=200,
        duration_ms=10,
    )
    p2 = create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="PLANNING",
        operation_key="planning:ch1:g0",
        input_fingerprint="f2",
        attempt_no=2,
        model="m",
        prompt_name="planner",
        prompt_version="v3",
    )
    finish_failed(session, p2, error_code="GENERATION_FAILED")  # 无 usage，不贡献 token
    g = create_attempt(
        session,
        user_id="u1",
        scope_type="TASK",
        scope_id="t1",
        task_id="t1",
        stage="GENERATING",
        operation_key="batch:1",
        input_fingerprint="gf1",
        attempt_no=1,
        model="m",
        prompt_name="generator",
        prompt_version="v2",
    )
    finish_success(
        session,
        g,
        usage={"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 2, "completion_tokens": 3},
        http_status=200,
        duration_ms=5,
    )
    session.commit()
    assert task_token_totals(session, task_id="t1") == {
        "PLANNING": {"cache_hit": 10, "cache_miss": 5, "output_tokens": 7},
        "GENERATING": {"cache_hit": 1, "cache_miss": 2, "output_tokens": 3},
    }
