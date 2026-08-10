"""限流集成测试（structure-contract 1.6）：429 + Retry-After + 维度 + 探针行为。"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _device_headers(device_id: str | None = None) -> dict[str, str]:
    return {"X-Device-ID": device_id or str(uuid.uuid4())}


def test_rate_limit_write_dimension_429_with_retry_after(tmp_path: Path) -> None:
    """写操作 60 req/min/device：阈值可下调以便测试——用 Settings 构造小阈值 app。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=3,
    )
    with TestClient(create_app(settings)) as client:
        headers = _device_headers()
        codes = []
        for _ in range(5):
            # POST /v1/decks 无路由 → 404；限流中间件在路由前执行
            resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
            codes.append(resp.status_code)
    assert codes[:3] == [404, 404, 404]  # 前 3 次通过（业务路由缺失 → 404）
    assert codes[3] == 429 and codes[4] == 429
    # Retry-After 响应头存在
    with TestClient(create_app(settings)) as client:
        headers = _device_headers()
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
    )
    with TestClient(create_app(settings)) as client:
        headers = {**_device_headers(), "Idempotency-Key": str(uuid.uuid4())}
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


def test_rate_limit_device_scope_isolated_per_device(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_iso.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=2,
    )
    with TestClient(create_app(settings)) as client:
        # 设备键按请求头隔离：同一设备的多次请求须用同一 X-Device-ID
        headers_a = _device_headers()
        headers_b = _device_headers()
        codes_a = [
            client.post("/v1/decks", json={}, headers=headers_a).status_code for _ in range(3)
        ]
        codes_b = [
            client.post("/v1/decks", json={}, headers=headers_b).status_code for _ in range(2)
        ]
    assert codes_a == [404, 404, 429]
    assert codes_b == [404, 404]  # 设备 B 不受设备 A 影响
