"""幂等域切换（P4-3）：IdempotencyKey 域从 device 切 user。

- 同一 idempotency_key 在不同用户下互不重放（user1 创建成功；user2 同 key 同 body
  正常执行而非重放，产生独立资源）。
- 同用户同 key 重放原响应（幂等语义保持）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "idem.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _user(client: TestClient, username: str, password: str) -> dict[str, str]:
    return auth_headers(client, username=username, password=password)


def test_idempotency_key_isolated_per_user(client: TestClient, tmp_path: Path) -> None:
    """同 key 跨用户互不重放：user2 正常执行（新资源）；同用户重放原响应。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    key = {"Idempotency-Key": str(uuid.uuid4())}
    r1 = client.post("/decks", json={"name": "D"}, headers={**h1, **key})
    assert r1.status_code == 201
    deck1 = r1.json()["deck_id"]
    # user2 同 key 同 body：不重放 user1 的响应，正常执行创建独立牌组
    r2 = client.post("/decks", json={"name": "D"}, headers={**h2, **key})
    assert r2.status_code == 201
    assert r2.json()["deck_id"] != deck1
    # user1 同 key 再发：重放首次响应（同一 deck_id）
    r3 = client.post("/decks", json={"name": "D"}, headers={**h1, **key})
    assert r3.status_code == 201
    assert r3.json()["deck_id"] == deck1
    # 幂等记录 2 行（每用户一行），user1 列表仍只有 1 个牌组
    engine = create_db_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() or 0
    assert rows == 2
    assert len(client.get("/decks", headers=h1).json()["items"]) == 1
    assert len(client.get("/decks", headers=h2).json()["items"]) == 1


def test_idempotency_replay_after_cross_user(client: TestClient) -> None:
    """并发占位/重放路径（P4 跟进 d）：user 域新行 user_id 非空，重放/冲突/并发三路径正常。"""
    h1 = _user(client, "user1", "pass-1111")
    key = {"Idempotency-Key": str(uuid.uuid4())}
    r1 = client.post("/decks", json={"name": "D"}, headers={**h1, **key})
    assert r1.status_code == 201
    # 同 key 异 body → 409（user 域谓词命中）
    conflict = client.post("/decks", json={"name": "OTHER"}, headers={**h1, **key})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
