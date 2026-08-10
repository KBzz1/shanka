"""Prometheus 指标（structure-contract 8.3；R-04 有意不进业务 OpenAPI，F1/R1 直接测试）。

F1 范围：HTTP 请求与限流指标 + 共享 registry；llm/generation/batch 指标在
V3B（llm_requests_total/llm_request_duration_seconds/llm_tokens_total）与
V5A（generation_tasks_total/generation_tasks_duration_seconds/batch_retry_total）
补充，全部注册到同一 REGISTRY。
"""

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest

router = APIRouter(tags=["observability"])

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "HTTP 请求总数", ["method", "path", "status"], registry=REGISTRY
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP 请求耗时", registry=REGISTRY
)
RATE_LIMIT_HIT_TOTAL = Counter("rate_limit_hit_total", "限流触发次数", ["scope"], registry=REGISTRY)


@router.get("/metrics")
def metrics() -> Response:
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
