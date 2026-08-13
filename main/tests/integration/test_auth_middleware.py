"""Bearer 认证中间件（DESIGN §4.3：401 + WWW-Authenticate: Bearer；豁免清单）。

client fixture 迁移后 schema：auth_sessions 查询（未知 token / 豁免外路径）与
/auth/register（豁免清单用例）都需要真实表结构。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "mw.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 豁免清单用例快速连发，隔离 IP 5 req/s 总闸门
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_missing_token_401_auth_required(client: TestClient) -> None:
    r = client.get("/decks", headers={"X-Device-ID": "11111111-1111-4111-8111-111111111111"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_malformed_token_401_auth_invalid(client: TestClient) -> None:
    r = client.get(
        "/decks",
        headers={
            "Authorization": "NotBearer xyz",
            "X-Device-ID": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_token_401_auth_invalid(client: TestClient) -> None:
    r = client.get(
        "/decks",
        headers={
            "Authorization": "Bearer never-seen-token",
            "X-Device-ID": "11111111-1111-4111-8111-111111111111",
        },
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_exempt_paths_do_not_require_bearer(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert (
        client.post("/auth/register", json={"username": "eve", "password": "pass-1234"}).status_code
        == 201
    )
    assert (
        client.post("/auth/login", json={"username": "eve", "password": "pass-1234"}).status_code
        == 200
    )
