"""HTTP 指标采集中间件（structure-contract 8.3）：http_requests_total + duration histogram。"""

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL


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
                method=request.method, path=request.url.path, status="500"
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.observe(time.monotonic() - start)
            raise
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=request.url.path, status=str(response.status_code)
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.observe(duration)
        return response
