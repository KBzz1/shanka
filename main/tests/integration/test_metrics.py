"""metrics 集成测试（structure-contract 8.3；R-04 不进业务 OpenAPI，直接测）。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _metric_value(text: str, metric_line: str) -> float:
    """从 Prometheus 文本提取指定 metric 行的数值；不存在返回 0。"""
    for line in text.splitlines():
        if line.startswith(metric_line):
            return float(line.split()[-1])
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
    assert _metric_value(after, healthz_line) - _metric_value(before, healthz_line) == 2.0


def test_metrics_counts_unhandled_exception_500(tmp_path: Path) -> None:
    """final review I-2：未处理异常 500 由最外层 ServerErrorMiddleware 兜底直发，
    不经过 MetricsMiddleware 的正常返回路径；dispatch 的异常分支必须显式计数。"""
    from fastapi import FastAPI

    from app.api import metrics as metrics_api
    from app.middleware.error_handler import register_exception_handlers
    from app.middleware.metrics_middleware import MetricsMiddleware

    probe = FastAPI()
    register_exception_handlers(probe)
    probe.include_router(metrics_api.router)
    probe.add_middleware(MetricsMiddleware)

    @probe.get("/boom")
    def boom() -> None:
        raise RuntimeError("internal detail")

    boom_line = 'http_requests_total{method="GET",path="/boom",status="500"}'
    with TestClient(probe, raise_server_exceptions=False) as client:
        before = _metric_value(client.get("/metrics").text, boom_line)
        resp = client.get("/boom")
        after = _metric_value(client.get("/metrics").text, boom_line)
    assert resp.status_code == 500
    # 进程级注册表跨测试累加：断言差值而非绝对值
    assert after - before == 1.0


def test_metrics_rate_limit_hit_recorded(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm3.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=1,
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶，显式调高隔离
    )
    write_line = 'rate_limit_hit_total{scope="write"}'
    from alembic import command
    from alembic.config import Config

    # Bearer 经 auth 中间件 → 需迁移后 auth_sessions 表
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    with TestClient(create_app(settings)) as client:
        headers = auth_headers(client)
        before = _metric_value(client.get("/metrics").text, write_line)
        client.post("/v1/decks", json={}, headers=headers)  # 首次（404，路由缺失）
        client.post("/v1/decks", json={}, headers=headers)  # 超限 → 429 → 指标 +1
        after = _metric_value(client.get("/metrics").text, write_line)
    # 进程级注册表跨测试累加：断言差值而非绝对值（rate_limit 测试先执行时 count ≥2 亦不受影响）
    assert after - before == 1.0
