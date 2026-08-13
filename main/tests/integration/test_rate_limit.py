"""限流集成测试（structure-contract 1.6）：429 + Retry-After + 维度 + 探针行为。"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _device_headers(client: TestClient, device_id: str | None = None) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ X-Device-ID。"""
    return {**auth_headers(client), "X-Device-ID": device_id or str(uuid.uuid4())}


def _upgrade(db_url: str) -> None:
    """限流测试库迁移：Bearer 经 auth 中间件 → 需 auth_sessions 表（双头窗口）。"""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")


def test_rate_limit_write_dimension_429_with_retry_after(tmp_path: Path) -> None:
    """写操作 60 req/min/device：阈值可下调以便测试——用 Settings 构造小阈值 app。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=3,
        rate_limit_ip_per_second=100,  # IP 维度隔离（双头窗口 register 计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        headers = _device_headers(client)
        codes = []
        for _ in range(5):
            # POST /v1/decks 无路由 → 404；限流中间件在路由前执行
            resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
            codes.append(resp.status_code)
    assert codes[:3] == [404, 404, 404]  # 前 3 次通过（业务路由缺失 → 404）
    assert codes[3] == 429 and codes[4] == 429
    # Retry-After 响应头存在
    with TestClient(create_app(settings)) as client:
        headers = _device_headers(client)
        for _ in range(4):
            client.post("/v1/decks", json={"name": "d"}, headers=headers)
        resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_pdf_dimension_hits_429(tmp_path: Path) -> None:
    """1.6 专门维度回归（fix round 1）：POST /pdfs 10 次/时/device 须生效。

    F-2 修复前 _scope 按 /v1/pdfs 判定（路由无前缀）→ 落入通用 write 维度；
    修复后走 pdf 维度——低阈值构造 app 验证 429。
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_pdf.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_pdf_per_hour=2,
        rate_limit_ip_per_second=100,  # IP 维度隔离（双头窗口 register 计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        headers = {**_device_headers(client), "Idempotency-Key": str(uuid.uuid4())}
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


def test_rate_limit_user_scope_isolated_per_user(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_iso.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=2,
        rate_limit_ip_per_second=100,  # IP 维度隔离（双头窗口 register 计入 IP）
    )
    _upgrade(settings.database_url)
    with TestClient(create_app(settings)) as client:
        # 业务桶键 = principal.user_id（P4-3）：每用户独立桶
        headers_a = {
            **auth_headers(client, "user1", "pass-1111"),
            "X-Device-ID": "11111111-1111-4111-8111-111111111111",
        }
        headers_b = {
            **auth_headers(client, "user2", "pass-2222"),
            "X-Device-ID": "11111111-1111-4111-8111-111111111111",
        }
        codes_a = [
            client.post("/v1/decks", json={}, headers=headers_a).status_code for _ in range(3)
        ]
        codes_b = [
            client.post("/v1/decks", json={}, headers=headers_b).status_code for _ in range(2)
        ]
    assert codes_a == [404, 404, 429]
    assert codes_b == [404, 404]  # user2 不受 user1 影响（同 X-Device-ID 也不串桶）
