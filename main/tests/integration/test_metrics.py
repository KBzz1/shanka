"""metrics 集成测试（structure-contract 8.3；R-04 不进业务 OpenAPI，直接测）。"""

import uuid
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
        database_url=f"sqlite:///{tmp_path / 'm.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,  # 本文件验证指标内容而非认证；/metrics 默认已收紧
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "# HELP http_requests_total" in resp.text
    assert "# TYPE http_requests_total counter" in resp.text


def test_metrics_tracks_http_requests(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm2.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,
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
        metrics_auth_exempt=True,
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


def test_metrics_histogram_buckets_accumulate(tmp_path: Path) -> None:
    """histogram 语义守卫（8.3 分位数证据的正确性前提）：_count/_sum/le= 桶随请求累积。

    全仓库此前零 bucket 断言；本用例锁定 PromQL p50/p99 可算的最小语义——
    计数一致、和严格为正、观测全部落入 +Inf 与已知桶。histogram 为无标签指标，
    /metrics 请求自身也计入，故用 generate_latest 进程内快照划定窗口，
    避免取数请求混入差值。
    """
    from prometheus_client import REGISTRY, generate_latest

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'h.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,
    )
    count_line = "http_request_duration_seconds_count"
    sum_line = "http_request_duration_seconds_sum"
    bucket_500ms = 'http_request_duration_seconds_bucket{le="0.5"}'
    bucket_inf = 'http_request_duration_seconds_bucket{le="+Inf"}'
    with TestClient(create_app(settings)) as client:
        before_text = generate_latest(REGISTRY).decode()
        client.get("/healthz")
        client.get("/healthz")
        after_text = generate_latest(REGISTRY).decode()
    assert _metric_value(after_text, count_line) - _metric_value(before_text, count_line) == 2.0
    assert _metric_value(after_text, sum_line) > _metric_value(before_text, sum_line)
    # 本地 /healthz 为纯内存响应：两次观测都应落在 0.5s 桶内
    bucket_diff = _metric_value(after_text, bucket_500ms) - _metric_value(before_text, bucket_500ms)
    inf_diff = _metric_value(after_text, bucket_inf) - _metric_value(before_text, bucket_inf)
    assert bucket_diff == 2.0, f"0.5s 桶累积 {bucket_diff}（预期 2）"
    assert inf_diff == 2.0, f"+Inf 桶累积 {inf_diff}（预期 2，计数与桶必须一致）"


def test_metrics_path_label_uses_route_template(tmp_path: Path) -> None:
    """path label 归一化守卫：动态路径记路由模板，原始 id 不产生高基数序列。"""
    from alembic import command
    from alembic.config import Config

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'p1.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,
    )
    # 未认证请求被 auth 中间件短路（先于路由匹配，path 记 "unmatched"），
    # 须持 Bearer 走到路由匹配：随机 id → 404，但路由模板已写入 scope。
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")
    template = 'http_requests_total{method="GET",path="/tasks/{task_id}",status="404"}'
    with TestClient(create_app(settings)) as client:
        headers = auth_headers(client)
        before = _metric_value(client.get("/metrics").text, template)
        r1 = client.get(f"/tasks/{uuid.uuid4()}", headers=headers)
        r2 = client.get(f"/tasks/{uuid.uuid4()}", headers=headers)
        after_text = client.get("/metrics").text
    assert r1.status_code == r2.status_code == 404
    assert _metric_value(after_text, template) - before == 2.0


def test_metrics_unmatched_paths_collapse_to_bounded_label(tmp_path: Path) -> None:
    """扫描器垃圾路径统一记 "unmatched"：不同原始路径不产生新序列（基数有界）。

    未认证请求经 auth 中间件短路即返回，无路由匹配可归一化——401 + unmatched
    是此类流量的真实形态。
    """
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'p2.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,
    )
    unmatched = 'http_requests_total{method="GET",path="unmatched",status="401"}'
    with TestClient(create_app(settings)) as client:
        before = _metric_value(client.get("/metrics").text, unmatched)
        client.get(f"/scanner-{uuid.uuid4()}")
        client.get(f"/scanner-{uuid.uuid4()}")
        after_text = client.get("/metrics").text
    assert _metric_value(after_text, unmatched) - before == 2.0


def test_llm_and_generation_histograms_use_contract_buckets(tmp_path: Path) -> None:
    """8.3 定制桶守卫：LLM 桶到 300s、生成任务桶到 1800s；默认桶上界（≤10s）已移除。

    默认桶（上限 10s）下生成任务耗时会整体落 +Inf、p99 失真——定制桶是
    「桶必须覆盖真实量级」的契约化表达；le="2.5" 为默认桶独有边界，出现即回归。
    暴露文本 label 按字母序（le 最前）、桶值为浮点（300.0），期望串据此构造。
    """
    from infra.metrics import GENERATION_TASKS_DURATION_SECONDS, LLM_REQUEST_DURATION_SECONDS

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'b.db'}",
        storage_path=tmp_path / "storage",
        metrics_auth_exempt=True,
    )
    llm_300 = 'llm_request_duration_seconds_bucket{le="300.0",model="deepseek-chat"}'
    llm_default_only = 'llm_request_duration_seconds_bucket{le="2.5",model="deepseek-chat"}'
    task_1800 = 'generation_tasks_duration_seconds_bucket{le="1800.0"}'
    task_default_only = 'generation_tasks_duration_seconds_bucket{le="2.5"}'
    with TestClient(create_app(settings)) as client:
        before_text = client.get("/metrics").text
        LLM_REQUEST_DURATION_SECONDS.labels(model="deepseek-chat").observe(45)
        GENERATION_TASKS_DURATION_SECONDS.observe(1500)
        after_text = client.get("/metrics").text
    # 45s 落 300s 桶、1500s 落 1800s 桶（默认桶下两者都只能进 +Inf）
    assert _metric_value(after_text, llm_300) - _metric_value(before_text, llm_300) == 1.0
    assert _metric_value(after_text, task_1800) - _metric_value(before_text, task_1800) == 1.0
    # 默认桶独有边界不应存在（桶集合在注册时固化，出现即有人改回默认桶）
    assert _metric_value(after_text, llm_default_only) == 0.0
    assert _metric_value(after_text, task_default_only) == 0.0
