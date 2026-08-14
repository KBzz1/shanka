"""共享测试基座（Progress F0）：隔离 Settings、临时 DB/存储、TestClient、可控时钟。

隔离测试配置：fixture 一律显式构造 Settings（临时目录），不加载任何 .env/环境配置；
时钟经 infra.clock 注入，服务代码只能通过 Clock 接口取时间。
"""

import weakref
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.main import create_app
from infra.clock import FrozenClock

REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/conftest.py → 仓库根


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}", storage_path=tmp_path / "storage"
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    """迁移后的真实 schema（alembic upgrade head），供 V1+ integration 测试使用。

    API 测试须在迁移后 schema 上跑：HTTP 测试文件内的 client fixture 各自
    alembic upgrade 到独立临时库（同一 REPO_ROOT 路径来源）。
    """
    from alembic import command
    from alembic.config import Config

    from infra.db.session import create_db_engine

    db_path = tmp_path / "migrated.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return create_db_engine(f"sqlite:///{db_path}")


# 已注册用户的 Bearer token 模块级缓存（P4-2 引入，P4-4 去设备头后仅 Bearer）。
# 键 (id(client), username)——id(client) 区分不同 DB 的 client（每个测试函数独立临时库）；
# 命中缓存直接返回，避免每次构造 headers 重复 Argon2id 计算（~100ms/次，无缓存全量
# +100s 级）。值带 weakref：client 对象 GC 后地址可被新 client 复用——weakref 已死则
# 视为未命中重新注册，杜绝「新 client 拿到旧库 token」的 401 污染。
_AUTH_TOKEN_CACHE: dict[tuple[int, str], tuple[weakref.ReferenceType[TestClient], str]] = {}


def auth_headers(
    client: TestClient,
    username: str = "alice",
    password: str = "secret-pass-1",
    email: str | None = None,
) -> dict[str, str]:
    """register 或 login 后返回 Bearer 头（P4-4 起 X-Device-ID 已退出，仅 Bearer）。

    缓存语义：同一 (client, username) 只做一次 register/login（token 会话为该测试
    client 的 DB 持有）；文件内 logout 撤销语义的测试请用文件内 helper（test_auth.py
    `_auth_headers` 每次重建会话，不经本缓存）。V2.4：登录键为 email，未显式传入时
    默认按 username 派生（username@example.com）。
    """
    email = email or f"{username}@example.com"
    cache_key = (id(client), username)
    entry = _AUTH_TOKEN_CACHE.get(cache_key)
    token = entry[1] if entry is not None and entry[0]() is client else None
    if token is None:
        r = client.post(
            "/auth/register",
            json={"username": username, "email": email, "password": password},
        )
        if r.status_code == 409:  # 同库同 email 已注册（跨 client 重放/共享库场景）
            r = client.post("/auth/login", json={"email": email, "password": password})
        assert r.status_code in (200, 201), r.text
        token = r.json()["access_token"]
        _AUTH_TOKEN_CACHE[cache_key] = (weakref.ref(client), token)
    return {"Authorization": f"Bearer {token}"}
