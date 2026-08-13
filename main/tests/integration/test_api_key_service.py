"""services.api_key 集成测试：保存/状态/覆盖规则/脱敏（真实 SQLite + mock transport）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
save_key 落库 ApiKey（FK → users）需 users 行前置——_seed_user 先建用户（HTTP 流中
由 /auth/register 建立，本层显式补种）。P4-4：Key 归属 user 域（device_id 不再写入）；
断言走 Core 列投影查询。
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
from infra.db.models import ApiKey, Base, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.api_key.service import get_status, masked, save_key

_TEST_KEY_HEX = "aa" * 32

_KEY_COLUMNS = (ApiKey.encrypted_key, ApiKey.status, ApiKey.masked_key, ApiKey.updated_at)


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


def _seed_user(session: Session, *, user_id: str) -> None:
    """users 行前置（FK 强制，HTTP 流由 /auth/register 自动建立）。"""
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                password_hash="x",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()


def _balance_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"is_available": True, "balance_infos": []})


def _key_row(session: Session, *, user_id: str) -> tuple[str, str, str, str] | None:
    """用户域 Key 行（Core 列投影——ApiKey 用户域行对 ORM 不可见）。"""
    row = session.execute(select(*_KEY_COLUMNS).where(ApiKey.user_id == user_id)).first()
    if row is None:
        return None
    return (row.encrypted_key, row.status, row.masked_key, row.updated_at)


def test_api_key_save_available_encrypts(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        result = save_key(
            session,
            user_id=user,
            api_key="sk-test123456",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "AVAILABLE"
    assert result["masked_key"] == "sk-****3456"
    with session_factory() as session:
        row = _key_row(session, user_id=user)
        assert row is not None
        assert "sk-test123456" not in row[0]  # 密文不含明文
        assert row[2] == "sk-****3456"
        assert row[1] == "AVAILABLE"


def test_api_key_save_invalid_not_saved(session_factory: Callable[[], Session]) -> None:
    user = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = _client(handler)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        result = save_key(
            session,
            user_id=user,
            api_key="sk-bad",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INVALID"
    with session_factory() as session:
        assert _key_row(session, user_id=user) is None  # 不落库


def test_api_key_save_insufficient_not_saved(session_factory: Callable[[], Session]) -> None:
    user = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"is_available": False, "balance_infos": []})

    client = _client(handler)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        result = save_key(
            session,
            user_id=user,
            api_key="sk-low",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INSUFFICIENT_BALANCE"
    with session_factory() as session:
        assert _key_row(session, user_id=user) is None


def test_api_key_save_invalid_does_not_overwrite_valid(
    session_factory: Callable[[], Session],
) -> None:
    """旧有效 Key 保护（6.2）：INVALID 不覆盖已存在有效 Key。"""
    user = _uuid()
    client_ok = _client(_balance_ok)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        save_key(
            session,
            user_id=user,
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
            user_id=user,
            api_key="sk-bad2",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client_bad,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert result["status"] == "INVALID"
    with session_factory() as session:
        row = _key_row(session, user_id=user)
        assert row is not None
        assert row[2] == "sk-****lid1"  # 仍为旧 Key


def test_api_key_save_available_overwrites(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        save_key(
            session,
            user_id=user,
            api_key="sk-first",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        save_key(
            session,
            user_id=user,
            api_key="sk-second",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        row = _key_row(session, user_id=user)
        assert row is not None
        assert row[2] == "sk-****cond"


def test_api_key_masked_rule() -> None:
    """脱敏规则唯一入口（services.api_key.masked）：sk-**** + 末 4 位；len<=4 全掩码。"""
    assert masked("sk-abcdefghijkl1234") == "sk-****1234"
    assert masked("short") == "sk-****hort"  # len>4 → 显示后 4 位
    assert masked("abc") == "sk-****"  # 短于 4 → 全掩码


def test_api_key_status_unknown_when_not_saved(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        result = get_status(session, user_id=user, encryption_key=bytes.fromhex(_TEST_KEY_HEX))
    assert result["status"] == "UNKNOWN"
    assert result["masked_key"] == ""


def test_api_key_status_returns_saved(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    client = _client(_balance_ok)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        save_key(
            session,
            user_id=user,
            api_key="sk-status1",
            encryption_key=bytes.fromhex(_TEST_KEY_HEX),
            client=client,
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        result = get_status(session, user_id=user, encryption_key=bytes.fromhex(_TEST_KEY_HEX))
    assert result["status"] == "AVAILABLE"
    assert result["masked_key"] == "sk-****tus1"


def test_api_key_save_upstream_unavailable_raises(session_factory: Callable[[], Session]) -> None:
    user = _uuid()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    client = _client(handler)
    with session_factory() as session:
        _seed_user(session, user_id=user)
        with pytest.raises(AppError) as excinfo:
            save_key(
                session,
                user_id=user,
                api_key="sk-x",
                encryption_key=bytes.fromhex(_TEST_KEY_HEX),
                client=client,
                now="2026-08-11T00:00:00.000Z",
            )
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE
