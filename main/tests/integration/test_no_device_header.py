"""X-Device-ID 已退出（DESIGN §4.4：普通请求删除该头）。

P4-T4：DeviceIDMiddleware 删除后，普通请求仅需 Bearer；X-Device-ID 头即使携带也被忽略
（不参与认证/授权/注册）；V2.3 起 devices 表已随不可逆迁移删除（无自动注册/刷新概念）。
"""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → V2.3 无 devices 表；同 test_auth.py）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "no_device.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 注册+请求连发，隔离 IP 5 req/s 总闸门
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def session_factory(client: TestClient) -> Iterator[sessionmaker[Session]]:
    """与 client 同一 DB 的 session_factory（表清单直接观测）。"""
    yield cast(FastAPI, client.app).state.session_factory


def test_no_device_header_required(client: TestClient) -> None:
    h = auth_headers(client)  # 仅 Bearer
    assert client.get("/decks", headers=h).status_code == 200


def test_device_header_ignored(client: TestClient) -> None:
    h = auth_headers(client)
    r = client.get("/decks", headers={**h, "X-Device-ID": "99999999-9999-4999-8999-999999999999"})
    assert r.status_code == 200  # 头被忽略，不参与认证/注册


def test_devices_table_not_auto_registered(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        tables = set(session.scalars(text("SELECT name FROM sqlite_master WHERE type='table'")))
    assert "devices" not in tables  # V2.3：devices 表已删除（无自动注册/刷新载体）
