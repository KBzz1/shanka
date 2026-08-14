"""api_key 用户域判别测试（P4-5；P3 债务闭环：用户域行 ORM 可见、旧 device 域行随 V2.3 删除）。

P3-T2 过渡期 ApiKey mapper 身份键曾改写为 device_id（V2.1 历史：用户域行对 ORM 不可见，
写侧因此走 Core 直写）；P4-5 移除改写，身份键回 user_id 元数据主键。本文件 HTTP 链两条
（PUT/status 用户域 + 跨用户隔离）T4 已绿——保留为回归守卫；ORM 可见性为判别
（mapper 移除前红、移除后绿）；V2.3 起旧 device 域行随不可逆迁移物理删除，判别翻转为
「列/表不存在 + ORM 只见 user 域行」。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey
from infra.db.session import create_db_engine, create_session_factory
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

_TEST_KEY_HEX = "aa" * 32


class FakeClient:
    """mock transport：validate_key 恒 AVAILABLE；close() no-op（不触网）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        pass

    def validate_key(self, api_key: str) -> str:
        return "AVAILABLE"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "key_user_domain.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶，显式调高隔离
        api_key_encryption_key=_TEST_KEY_HEX,
    )
    monkeypatch.setattr("app.api.api_key.DeepSeekClient", FakeClient)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str) -> str:
    """注册用户（username）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def test_put_and_status_user_domain(client: TestClient, tmp_path: Path) -> None:
    """PUT 落库（用户域行 user_id 非空）→ GET status AVAILABLE（T4 已绿守卫）。"""
    user = auth_headers(client)
    resp = client.put("/api-key", json={"api_key": "sk-test-abcd1234"}, headers={**user, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"
    assert resp.json()["masked_key"] == "sk-****1234"

    engine = create_db_engine(f"sqlite:///{tmp_path / 'key_user_domain.db'}")
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(api_keys)"))}
        row = conn.execute(text("SELECT user_id FROM api_keys")).one()
    assert "device_id" not in cols  # V2.3：api_keys 无 device_id 列（随不可逆迁移删除）
    assert row[0] == _user_id(tmp_path / "key_user_domain.db", "alice")

    st = client.get("/api-key/status", headers=user)
    assert st.status_code == 200
    assert st.json()["status"] == "AVAILABLE"
    assert st.json()["masked_key"] == "sk-****1234"


def test_cross_user_key_isolation(client: TestClient) -> None:
    """用户域隔离：user1 的 Key 对 user2 不可见（status UNKNOWN）。"""
    h1 = auth_headers(client, username="keyuser1", password="pass-1111")
    h2 = auth_headers(client, username="keyuser2", password="pass-2222")
    resp = client.put("/api-key", json={"api_key": "sk-test-aaaa1111"}, headers={**h1, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"
    st = client.get("/api-key/status", headers=h2)
    assert st.status_code == 200
    assert st.json()["status"] == "UNKNOWN"  # 用户域隔离：user2 看不到 user1 的 Key


def test_user_domain_row_orm_visible(client: TestClient, tmp_path: Path) -> None:
    """判别（P4-5）：mapper 移除后用户域行经 ORM session.get 可见（移除前红）。"""
    user = auth_headers(client)
    resp = client.put("/api-key", json={"api_key": "sk-test-orm0001"}, headers={**user, **_idem()})
    assert resp.status_code == 200
    db_path = tmp_path / "key_user_domain.db"
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        row = session.get(ApiKey, _user_id(db_path, "alice"))
    assert row is not None  # 身份键回 user_id 元数据主键：用户域行 ORM 可见
    assert "sk-test-orm0001" not in row.encrypted_key  # 红线 4：密文不含明文


def test_legacy_device_row_invisible_to_orm(client: TestClient, tmp_path: Path) -> None:
    """判别（P4-5，V2.3 语义翻转）：旧 device 域行已随不可逆迁移物理删除，ORM 只见 user 域行。

    V2.3 起 devices 表与 api_keys.device_id 列不存在——旧 device 域行无法再被 raw
    INSERT 构造（D-06 语义由「不可见」升级为「不存在」；V2.1 历史：mapper 身份键为
    device_id 时旧行可被 ORM 返回，本测试曾断言 [None] 占位）。
    """
    auth_headers(client)  # users 行前置（HTTP 流）；api_keys 无旧 device 域行可插
    db_path = tmp_path / "key_user_domain.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(api_keys)"))}
        tables = {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "device_id" not in cols  # V2.3：api_keys 无 device_id 列
    assert "devices" not in tables  # V2.3：devices 表已 drop
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        assert session.query(ApiKey).all() == []  # ORM 只见 user 域行（旧 device 域行已删除）
