"""services.api_key 集成测试：保存/状态/覆盖规则/脱敏（真实 SQLite + mock transport）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
save_key 落库 ApiKey（FK → devices）需 devices 行前置——_seed_device 先建设备
（HTTP 流中由 F1 设备中间件自动建立，本层显式补种）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, Base, Device
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.api_key.service import get_status, save_key

_TEST_KEY_HEX = "aa" * 32


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'key.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _settings() -> Settings:
    return Settings(api_key_encryption_key=_TEST_KEY_HEX)


def _uuid() -> str:
    return str(uuid.uuid4())


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> DeepSeekClient:
    return DeepSeekClient(_settings(), transport=httpx.MockTransport(handler))


def _seed_device(session: Session, *, device_id: str) -> None:
    """devices 行前置（FK 强制，HTTP 流由 F1 设备中间件自动建立）。"""
    if session.scalar(select(Device).where(Device.device_id == device_id)) is None:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()


def _balance_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"is_available": True, "balance_infos": []})


def test_api_key_save_available_encrypts(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        result = save_key(
            session,
            device_id=device,
            api_key="sk-test123456",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "AVAILABLE"
    assert result["masked_key"] == "sk-****3456"
    with session_factory() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.device_id == device))
        assert row is not None
        assert "sk-test123456" not in row.encrypted_key  # 密文不含明文
        assert row.masked_key == "sk-****3456"
        assert row.status == "AVAILABLE"


def test_api_key_save_invalid_not_saved(session_factory: Callable[[], Session]) -> None:
    device = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = _client(handler)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        result = save_key(
            session,
            device_id=device,
            api_key="sk-bad",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INVALID"
    with session_factory() as session:
        assert session.scalar(select(ApiKey).where(ApiKey.device_id == device)) is None  # 不落库


def test_api_key_save_insufficient_not_saved(session_factory: Callable[[], Session]) -> None:
    device = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"is_available": False, "balance_infos": []})

    client = _client(handler)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        result = save_key(
            session,
            device_id=device,
            api_key="sk-low",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INSUFFICIENT_BALANCE"
    with session_factory() as session:
        assert session.scalar(select(ApiKey).where(ApiKey.device_id == device)) is None


def test_api_key_save_invalid_does_not_overwrite_valid(
    session_factory: Callable[[], Session],
) -> None:
    """旧有效 Key 保护（6.2）：INVALID 不覆盖已存在有效 Key。"""
    device = _uuid()
    client_ok = _client(_balance_ok)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        save_key(
            session,
            device_id=device,
            api_key="sk-valid1",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client_ok,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()

    # 再提交 INVALID Key
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client_bad = _client(handler)
    with session_factory() as session:
        result = save_key(
            session,
            device_id=device,
            api_key="sk-bad2",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client_bad,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INVALID"
    with session_factory() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.device_id == device))
        assert row is not None
        assert row.masked_key == "sk-****lid1"  # 仍为旧 Key


def test_api_key_save_available_overwrites(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        save_key(
            session,
            device_id=device,
            api_key="sk-first",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        save_key(
            session,
            device_id=device,
            api_key="sk-second",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        row = session.scalar(select(ApiKey).where(ApiKey.device_id == device))
        assert row is not None
        assert row.masked_key == "sk-****cond"


def test_api_key_status_unknown_when_not_saved(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        result = get_status(session, device_id=device, encryption_key=bytes.fromhex(_TEST_KEY_HEX))
    assert result["status"] == "UNKNOWN"
    assert result["masked_key"] == ""


def test_api_key_status_returns_saved(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        save_key(
            session,
            device_id=device,
            api_key="sk-status1",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        result = get_status(session, device_id=device, encryption_key=bytes.fromhex(_TEST_KEY_HEX))
    assert result["status"] == "AVAILABLE"
    assert result["masked_key"] == "sk-****tus1"


def test_api_key_save_upstream_unavailable_raises(session_factory: Callable[[], Session]) -> None:
    device = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    client = _client(handler)
    with session_factory() as session:
        _seed_device(session, device_id=device)
        with pytest.raises(AppError) as excinfo:
            save_key(
                session,
                device_id=device,
                api_key="sk-x",
                encryption_key=bytes.fromhex(_TEST_KEY_HEX),
                client=client,
                now="2026-08-11T00:00:00.000Z",
            )
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE
