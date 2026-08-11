"""请求日志中间件（structure-contract 8.1）：INFO 请求进出；不记录请求体（1.5 红线）。

记录字段：method/path/status/duration_ms + request_id/device_id 上下文。
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("app.middleware.logging")


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.monotonic()
        try:
            response = await call_next(request)
            self._log(request, response.status_code, start)
            return response
        except Exception as exc:
            logger.error(
                "request failed",
                extra={
                    "request_id": getattr(request.state, "request_id", ""),
                    "device_id": getattr(request.state, "device_id", ""),
                    "error_code": "INTERNAL_ERROR",
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                },
                exc_info=exc,
            )
            raise

    def _log(self, request: Request, status: int, start: float) -> None:
        logger.info(
            "request complete",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "device_id": getattr(request.state, "device_id", ""),
                "error_code": getattr(request.state, "error_code", ""),
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": int((time.monotonic() - start) * 1000),
            },
        )
