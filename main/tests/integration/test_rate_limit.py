"""限流集成测试（structure-contract 1.6）：429 + Retry-After + 维度 + 探针行为。"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _user_headers(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _upgrade(db_url: str) -> None:
    """限流测试库迁移：Bearer 经 auth 中间件 → 需 auth_sessions 表。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def test_rate_limit_write_dimension_429_with_retry_after(tmp_path: Path) -> None:
    """写操作 60 req/min/user：阈值可下调以便测试——用 Settings 构造小阈值 app。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=3,
        rate_limit_ip_per_second=100,  # IP 维度隔离（register 请求计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        headers = _user_headers(client)
        codes = []
        for _ in range(5):
            # POST /v1/decks 无路由 → 404；限流中间件在路由前执行
            resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
            codes.append(resp.status_code)
    assert codes[:3] == [404, 404, 404]  # 前 3 次通过（业务路由缺失 → 404）
    assert codes[3] == 429 and codes[4] == 429
    # Retry-After 响应头存在
    with TestClient(create_app(settings)) as client:
        headers = _user_headers(client)
        for _ in range(4):
            client.post("/v1/decks", json={"name": "d"}, headers=headers)
        resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_pdf_dimension_hits_429(tmp_path: Path) -> None:
    """1.6 专门维度回归（fix round 1）：POST /pdfs 10 次/时/user 须生效。

    F-2 修复前 _scope 按 /v1/pdfs 判定（路由无前缀）→ 落入通用 write 维度；
    修复后走 pdf 维度——低阈值构造 app 验证 429。
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_pdf.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_pdf_per_hour=2,
        rate_limit_ip_per_second=100,  # IP 维度隔离（register 请求计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        headers = {**_user_headers(client), "Idempotency-Key": str(uuid.uuid4())}
        codes = []
        for _ in range(4):
            resp = client.post(
                "/pdfs",
                files={"file": ("a.pdf", b"x", "application/pdf")},
                headers=headers,
            )
            codes.append(resp.status_code)
    assert codes[:2] == [400, 400]  # 限流通过 → 上传三重校验失败 400
    assert codes[2] == 429 and codes[3] == 429  # pdf 维度限流


def test_rate_limit_ip_dimension_blocks(tmp_path: Path) -> None:
    """IP 5 req/s（全部接口）：用低阈值 app 验证。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_ip.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=2,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/healthz").status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429  # IP 维度覆盖探针（1.6 表"全部接口"）


def test_rate_limit_ip_dimension_covers_unauthenticated_traffic(tmp_path: Path) -> None:
    """fix round 1：IP 总闸门运行于 Auth 外层——未认证流量（无 Bearer）也计入 IP 桶。

    P4-3 将 Auth 移出 RateLimit 外层后，未认证请求在 Auth 401 短路，不再经过任何
    业务桶；契约 1.6「IP 5 req/s：全部接口」要求 IP 桶仍覆盖该流量（含 DB 读放大
    面防护）——超限后 429 先于 Auth 401 短路。
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_ip_unauth.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=2,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/decks").status_code for _ in range(3)]
    assert codes == [401, 401, 429]  # 未认证流量计入 IP 桶：前 2 次放行至 Auth 401，第 3 次 429
    with TestClient(create_app(settings)) as client:
        for _ in range(2):
            client.get("/decks")
        resp = client.get("/decks")
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert int(resp.headers["Retry-After"]) > 0


def test_rate_limit_user_scope_isolated_per_user(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_iso.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=2,
        rate_limit_ip_per_second=100,  # IP 维度隔离（register 请求计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        # 业务桶键 = principal.user_id（P4-3）：每用户独立桶
        headers_a = auth_headers(client, "user1", "pass-1111")
        headers_b = auth_headers(client, "user2", "pass-2222")
        codes_a = [
            client.post("/v1/decks", json={}, headers=headers_a).status_code for _ in range(3)
        ]
        codes_b = [
            client.post("/v1/decks", json={}, headers=headers_b).status_code for _ in range(2)
        ]
    assert codes_a == [404, 404, 429]
    assert codes_b == [404, 404]  # user2 不受 user1 影响（键已切 user 域）
