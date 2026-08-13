"""幂等原语集成测试（1.3/2.12）：重放、冲突、并发占位、回滚、同事务。"""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app.middleware.idempotency import execute_idempotent, request_body_hash
from infra.db.models import Base
from infra.db.session import create_db_engine, create_session_factory


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    return factory


def _side_effect_rows(session_factory: Callable[[], Session]) -> int:
    with session_factory() as s:
        return s.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() or 0


def test_idempotency_fresh_executes_and_records(session_factory: Callable[[], Session]) -> None:
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    with session_factory() as session:
        replayed, status, body = execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=str(uuid.uuid4()),
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    assert replayed is False
    assert status == 201
    assert body == {"created": True}
    assert len(calls) == 1
    assert _side_effect_rows(session_factory) == 1


def test_idempotency_replay_returns_first_response(session_factory: Callable[[], Session]) -> None:
    key = str(uuid.uuid4())
    body = b'{"name":"d"}'
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    with session_factory() as session:
        execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(body),
            fn=biz,
        )
        session.commit()
    with session_factory() as session:
        replayed, status, body_out = execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(body),
            fn=biz,
        )
        session.commit()
    assert replayed is True
    assert status == 201
    assert body_out == {"created": True}
    assert len(calls) == 1  # 业务只执行一次


def test_idempotency_body_mismatch_raises_conflict(session_factory: Callable[[], Session]) -> None:
    from app.errors import AppError, ErrorCode

    key = str(uuid.uuid4())

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        return 201, {"created": True}

    with session_factory() as session:
        execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"OTHER"}'),
            fn=biz,
        )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_non_2xx_not_recorded_and_retried(
    session_factory: Callable[[], Session],
) -> None:
    """非 2xx 响应不落幂等记录；同键重试重新执行（1.3/2.12 仅记录成功）。"""
    key = str(uuid.uuid4())
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 422, {"detail": "invalid"}

    with session_factory() as session:
        replayed, status, body = execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    assert replayed is False
    assert status == 422
    assert body == {"detail": "invalid"}
    assert _side_effect_rows(session_factory) == 0  # 未落库
    # 同键重试 → 重新执行（不重放错误响应）
    with session_factory() as session:
        replayed, status, body = execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    assert replayed is False
    assert status == 422
    assert body == {"detail": "invalid"}
    assert len(calls) == 2  # 业务重新执行
    assert _side_effect_rows(session_factory) == 0


def test_idempotency_concurrent_same_key_single_effect(
    session_factory: Callable[[], Session],
) -> None:
    """并发同键：唯一约束占位，后到者回滚并重读重放（database-design 2.12）。"""
    import threading

    key = str(uuid.uuid4())
    body = b'{"name":"d"}'
    calls: list[str] = []
    results: list[tuple[bool, int, dict[str, object]]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    def worker() -> None:
        with session_factory() as session:
            try:
                out = execute_idempotent(
                    session,
                    user_id="user-1",
                    path="/v1/decks",
                    idempotency_key=key,
                    request_body_hash=request_body_hash(body),
                    fn=biz,
                )
                session.commit()
                with lock:
                    results.append(out)
            except Exception as exc:
                # 记录并重抛（与 logging.py 同模式）：断言 errors==[] 暴露线程异常
                with lock:
                    errors.append(exc)
                raise

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # 线程异常 → 直接暴露测试失败
    assert len(calls) == 1  # 业务副作用仅一次
    assert _side_effect_rows(session_factory) == 1  # 幂等记录仅一行
    # 两个线程都拿到结果（一个 fresh、一个 replayed）
    assert len(results) == 2
    fresh = [r for r in results if r[0] is False]
    replayed = [r for r in results if r[0] is True]
    assert len(fresh) == 1 and len(replayed) == 1


def test_idempotency_rollback_releases_claim(session_factory: Callable[[], Session]) -> None:
    """业务失败回滚：幂等记录一并回滚，同键重试重新执行（1.3 仅记录成功）。"""
    key = str(uuid.uuid4())
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        raise RuntimeError("biz failed")

    with session_factory() as session:
        with pytest.raises(RuntimeError):
            execute_idempotent(
                session,
                user_id="user-1",
                path="/v1/decks",
                idempotency_key=key,
                request_body_hash=request_body_hash(b"{}"),
                fn=biz,
            )
        session.rollback()
    assert _side_effect_rows(session_factory) == 0

    # 同键重试 → 重新执行（无记录 → fresh）
    def biz2(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("y")
        return 200, {"ok": True}

    with session_factory() as session:
        replayed, _status, _body = execute_idempotent(
            session,
            user_id="user-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b"{}"),
            fn=biz2,
        )
        session.commit()
    assert replayed is False
    assert calls == ["x", "y"]


def test_idempotency_flush_conflict_backstop_replays(tmp_path: Path) -> None:
    """flush 冲突兜底路径回归（review M-3，确定性构造）。

    用无 BEGIN IMMEDIATE 的引擎 + WAL 读快照：A 未提交写占位 → B SELECT 读快照无 →
    B fn → B flush 阻塞至 A commit → IntegrityError → rollback 重读重放。
    事件链保证 B 的 flush 恒晚于 A 的 flush（b_started 由 B 的 fn 在 flush 前置位，
    而 A 在 b_started 后才 commit），无时序竞争。
    """
    import threading

    engine = create_engine(
        f"sqlite:///{tmp_path / 'backstop.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure_wal(dbapi_connection: Any, connection_record: Any) -> None:
        # WAL：B 的 SELECT 可读 A 未提交写之前的快照（无 begin 事件 → 走 flush 冲突路径）
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    key = str(uuid.uuid4())
    body = b'{"name":"d"}'
    a_flushed = threading.Event()  # A 已 flush 占写锁（execute_idempotent 返回后置位）
    b_started = threading.Event()  # B 已完成 fn（flush 即将阻塞）
    calls: list[str] = []
    results: list[tuple[bool, int, dict[str, object]]] = []
    errors: list[BaseException] = []

    def biz_a(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("a")
        return 201, {"created": "a"}

    def biz_b(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("b")
        b_started.set()
        return 201, {"created": "b"}

    def worker_a() -> None:
        with factory() as session:
            try:
                out = execute_idempotent(
                    session,
                    user_id="user-1",
                    path="/v1/decks",
                    idempotency_key=key,
                    request_body_hash=request_body_hash(body),
                    fn=biz_a,
                )
                # execute_idempotent 返回 = flush 已执行、写锁在手 → 再通知主线程
                a_flushed.set()
                assert b_started.wait(timeout=10), "B 未在超时内进入 fn"
                session.commit()
                results.append(out)
            except Exception as exc:
                errors.append(exc)
                raise

    thread_a = threading.Thread(target=worker_a)
    thread_a.start()
    try:
        assert a_flushed.wait(timeout=10), "A 未在超时内 flush"
        with factory() as session:
            replayed, status, body_out = execute_idempotent(
                session,
                user_id="user-1",
                path="/v1/decks",
                idempotency_key=key,
                request_body_hash=request_body_hash(body),
                fn=biz_b,
            )
            session.commit()
    finally:
        thread_a.join(timeout=15)

    assert errors == []
    assert not thread_a.is_alive()
    assert replayed is True
    assert status == 201
    assert body_out == {"created": "a"}  # 重放 A 的响应，而非 B 的
    assert calls == ["a", "b"]  # 双方业务各执行一次
    assert results == [(False, 201, {"created": "a"})]
    assert _side_effect_rows(factory) == 1  # 幂等记录仅 A 一行
