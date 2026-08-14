"""请求日志中间件（structure-contract 8.1）：INFO 请求进出；不记录请求体（1.5 红线）。

记录字段：method/path/status/duration_ms + request_id/user_id 上下文（P4-6：身份字段
切 user_id——principal 存在时记录 principal.user_id，匿名请求省略该字段）。

敏感路径（/auth/register /auth/login /api-key）：本中间件从不记录 headers（Authorization/
密码/token 绝不入日志），BodyCapture 只提供幂等 body hash、不落 body 原文——敏感路径
不额外记录任何 body/header，红线 4 固化。
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("app.middleware.logging")


def _identity_extra(request: Request, extra: dict[str, object]) -> dict[str, object]:
    """身份字段（P4-6）：principal 存在时附 user_id；匿名请求省略。"""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        extra["user_id"] = principal.user_id
    return extra


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
                extra=_identity_extra(
                    request,
                    {
                        "request_id": getattr(request.state, "request_id", ""),
                        "error_code": "INTERNAL_ERROR",
                        "method": request.method,
                        "path": request.url.path,
                        "status": 500,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    },
                ),
                exc_info=exc,
            )
            raise

    def _log(self, request: Request, status: int, start: float) -> None:
        logger.info(
            "request complete",
            extra=_identity_extra(
                request,
                {
                    "request_id": getattr(request.state, "request_id", ""),
                    "error_code": getattr(request.state, "error_code", ""),
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                },
            ),
        )
