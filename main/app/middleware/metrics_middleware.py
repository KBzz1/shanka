"""HTTP 指标采集中间件（structure-contract 8.3）：http_requests_total + duration histogram。

path label 归一化为路由模板（如 /tasks/{task_id}），动态段不产生高基数序列；
未匹配任何路由的请求（404 探测/扫描器垃圾路径）统一记 "unmatched"，基数有界。
"""

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL


def _metric_path(request: Request) -> str:
    """FastAPI 路由全匹配时把 route 写入 scope（405 部分/404 无匹配不写）→ 取其模板。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # final review I-2：未处理异常 500 由外层 ServerErrorMiddleware 兜底直发，
            # 不经本中间件正常返回路径（8.3 核心指标会漏计 500）；此处显式计数/计时后
            # 继续传播异常。
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method, path=_metric_path(request), status="500"
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.observe(time.monotonic() - start)
            raise
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=_metric_path(request), status=str(response.status_code)
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.observe(duration)
        return response
