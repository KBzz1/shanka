"""IP 限流总闸门（structure-contract 1.6「IP 5 req/s：全部接口」；P4-3 review fix round 1）。

Auth 移出 RateLimit 外层后（P4-3），未认证流量（缺失/无效/撤销/过期 Bearer）在 Auth
401 短路，不再经过任何业务桶——契约 1.6 字面违约 + 未节流 DB 读放大面（T2 基线时该
流量被 IP 桶覆盖，属回归）。本中间件只做 IP 维度（键=client_ip，覆盖全部接口含探针与
未认证流量），运行于 Auth 外层；业务维度（write/api_key/samples/pdf/auth）仍归
rate_limit.py（Auth 内层，键=principal.user_id 或 IP）。

实现：内存固定窗口（单实例 MVP；多实例演进时换共享存储，业务逻辑不变——见契约 4.4
定式）。超限：429 RATE_LIMITED + Retry-After 响应头 + scope="ip" 指标（契约 8.3）。

时钟：构造可注入 clock（RateLimiter 透传）——测试侧固定时钟消除 1 秒窗口边界
flakiness；生产装配不传（默认 time.monotonic）。
"""

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.metrics import RATE_LIMIT_HIT_TOTAL
from app.config import Settings
from app.errors import AppError, ErrorCode, http_status
from app.middleware.rate_limit import ClockLike, RateLimiter

logger = logging.getLogger(__name__)


class IpRateLimitMiddleware(BaseHTTPMiddleware):
    """IP 5 req/s 总闸门（1.6「全部接口」）：运行于 Auth 外层，未认证流量同样限流。"""

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        *,
        clock: Callable[[], float] | ClockLike | None = None,
    ) -> None:
        super().__init__(app)
        self._ip_limiter = RateLimiter(
            limit=settings.rate_limit_ip_per_second,
            window_seconds=1,
            clock=clock if clock is not None else time.monotonic,
        )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = self._ip_limiter.check(client_ip)
        if not allowed:
            return self._rate_limited(request, retry_after)
        return await call_next(request)

    def _rate_limited(self, request: Request, retry_after: int) -> JSONResponse:
        logger.warning(
            "rate limited",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "user_id": "",  # IP 层运行于 Auth 外层，身份未解析
                "error_code": "RATE_LIMITED",
                "path": request.url.path,
            },
        )
        # rate_limit_hit_total 指标（structure-contract 8.3 scope 表含 ip）
        RATE_LIMIT_HIT_TOTAL.labels(scope="ip").inc()
        response = JSONResponse(
            status_code=http_status(ErrorCode.RATE_LIMITED),
            content=AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试").to_response(),
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
