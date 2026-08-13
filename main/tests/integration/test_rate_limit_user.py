"""限流用户域切换（P4-3）：业务维度键 user_id、auth IP 桶、login 用户名桶。

- 业务桶（write/api_key/samples/pdf）键从 X-Device-ID 切 principal.user_id：
  user1 写超限 429 不影响 user2。
- auth 维度（structure-contract 1.6/8.3）：POST /auth/register|/auth/login 按 IP
  限流，429 + Retry-After。
- login 用户名桶（service 层限流）：同用户名多次登录（含失败）超限 → 429 +
  Retry-After，其他用户名不受影响。
"""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _upgrade(db_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def _make_client(tmp_path: Path, name: str, **overrides: Any) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / f'{name}.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
        **overrides,
    )
    _upgrade(settings.database_url)
    return TestClient(create_app(settings))


def test_write_dimension_isolated_per_user(tmp_path: Path) -> None:
    """业务维度键 = user_id：user1 超限 429 不影响 user2（同 X-Device-ID 也不串桶）。"""
    with _make_client(tmp_path, "rl_user", rate_limit_write_per_minute=2) as client:
        # 两用户共用一个 X-Device-ID——键已切 user 域，不再按设备串桶
        h1 = {
            **auth_headers(client, "user1", "pass-1111"),
            "X-Device-ID": "99999999-9999-4999-8999-999999999999",
        }
        h2 = {
            **auth_headers(client, "user2", "pass-2222"),
            "X-Device-ID": "99999999-9999-4999-8999-999999999999",
        }
        codes_a = [client.post("/v1/decks", json={}, headers=h1).status_code for _ in range(3)]
        codes_b = [client.post("/v1/decks", json={}, headers=h2).status_code for _ in range(2)]
    assert codes_a == [404, 404, 429]  # 无路由 404：限流在路由前
    assert codes_b == [404, 404]  # user2 不受 user1 影响


def test_auth_ip_bucket_429_with_retry_after(tmp_path: Path) -> None:
    """auth 维度：/auth/register 按 IP 限流，超限 429 + Retry-After + RATE_LIMITED。"""
    with _make_client(tmp_path, "rl_auth", rate_limit_auth_per_hour=3) as client:
        codes = []
        for i in range(4):
            resp = client.post(
                "/auth/register",
                json={"username": f"user{i}", "password": f"pass-{i}111"},
            )
            codes.append(resp.status_code)
        blocked = client.post("/auth/register", json={"username": "user5", "password": "pass-5111"})
    assert codes == [201, 201, 201, 429]
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) > 0


def test_login_username_bucket_429_with_retry_after(tmp_path: Path) -> None:
    """login 用户名桶（service 层）：同用户名多次登录超限 → 429 + Retry-After；
    其他用户名不受影响。"""
    with _make_client(tmp_path, "rl_login", rate_limit_login_username_per_hour=3) as client:
        # 注册 2 个用户（auth IP 桶默认 20/h 不干扰）
        assert (
            client.post(
                "/auth/register", json={"username": "bob", "password": "bob-pass-1"}
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/auth/register", json={"username": "eve", "password": "eve-pass-1"}
            ).status_code
            == 201
        )
        codes = []
        for _ in range(3):
            resp = client.post("/auth/login", json={"username": "bob", "password": "wrong-pass"})
            codes.append(resp.status_code)
        blocked = client.post("/auth/login", json={"username": "bob", "password": "wrong-pass"})
        other = client.post("/auth/login", json={"username": "eve", "password": "wrong-pass"})
    assert codes == [401, 401, 401]  # 密码错误（不暴露存在性）
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert int(blocked.headers["Retry-After"]) > 0
    assert other.status_code == 401  # 其他用户名不受 bob 桶影响


def test_login_username_bucket_success_also_counts(tmp_path: Path) -> None:
    """成功登录同样计入用户名桶：成功 2 次 + 失败 1 次后超限 → 429。"""
    with _make_client(tmp_path, "rl_login2", rate_limit_login_username_per_hour=3) as client:
        assert (
            client.post(
                "/auth/register", json={"username": "bob", "password": "bob-pass-1"}
            ).status_code
            == 201
        )
        ok1 = client.post("/auth/login", json={"username": "bob", "password": "bob-pass-1"})
        assert ok1.status_code == 200
        assert (
            client.post(
                "/auth/login", json={"username": "bob", "password": "wrong-pass"}
            ).status_code
            == 401
        )
        ok2 = client.post("/auth/login", json={"username": "bob", "password": "bob-pass-1"})
        assert ok2.status_code == 200
        blocked = client.post("/auth/login", json={"username": "bob", "password": "bob-pass-1"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
