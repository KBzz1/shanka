"""幂等原语集成测试（1.3/2.12）：重放、冲突、并发占位、回滚、同事务。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text
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
            device_id="dev-1",
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
            device_id="dev-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(body),
            fn=biz,
        )
        session.commit()
    with session_factory() as session:
        replayed, status, body_out = execute_idempotent(
            session,
            device_id="dev-1",
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
            device_id="dev-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        execute_idempotent(
            session,
            device_id="dev-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b'{"name":"OTHER"}'),
            fn=biz,
        )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


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
                    device_id="dev-1",
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
                device_id="dev-1",
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
            device_id="dev-1",
            path="/v1/decks",
            idempotency_key=key,
            request_body_hash=request_body_hash(b"{}"),
            fn=biz2,
        )
        session.commit()
    assert replayed is False
    assert calls == ["x", "y"]
