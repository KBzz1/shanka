"""metrics 集成测试（structure-contract 8.3；R-04 不进业务 OpenAPI，直接测）。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _counter_value(text: str, line_prefix: str) -> float:
    """解析文本格式中某指标行的数值；进程级注册表跨测试累加，未出现该行视为 0。"""
    for line in text.splitlines():
        if line.startswith(line_prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_endpoint_returns_prometheus_text(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm.db'}", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "# HELP http_requests_total" in resp.text
    assert "# TYPE http_requests_total counter" in resp.text


def test_metrics_tracks_http_requests(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm2.db'}", storage_path=tmp_path / "storage"
    )
    healthz_line = 'http_requests_total{method="GET",path="/healthz",status="200"}'
    with TestClient(create_app(settings)) as client:
        before = client.get("/metrics").text
        client.get("/healthz")
        client.get("/healthz")
        after = client.get("/metrics").text
    # 指标为进程级注册表（跨测试累加，如 test_device_auth 的 /healthz）：断言差值而非绝对值
    assert _counter_value(after, healthz_line) - _counter_value(before, healthz_line) == 2.0


def test_metrics_rate_limit_hit_recorded(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm3.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        import uuid

        headers = {"X-Device-ID": str(uuid.uuid4())}
        client.post("/v1/decks", json={}, headers=headers)  # 首次（404，路由缺失）
        client.post("/v1/decks", json={}, headers=headers)  # 超限 → 429 → 指标 +1
        resp = client.get("/metrics")
    assert 'rate_limit_hit_total{scope="write"} 1.0' in resp.text
