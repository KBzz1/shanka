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
        response = await call_next(request)
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, path=request.url.path, status=str(response.status_code)
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.observe(duration)
        return response
