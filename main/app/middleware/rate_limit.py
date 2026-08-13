"""限流中间件——业务维度（structure-contract 1.6；红线 3）。

维度判定（1.6 表）：
- 写操作 60 req/min/user：全部写接口（POST/PUT/PATCH/DELETE），
  被专门维度覆盖的接口（/api-key、/samples、/pdfs）除外。
- PUT /api-key 10 次/时/user；POST /samples 20 次/时/user；POST /pdfs 10 次/时/user。
- auth 维度（P4-3 切换）：POST /auth/register|/auth/login 20 次/时/IP，键=client_ip；
  login 用户名桶（10 次/时/用户名）在 auth service 内复用 RateLimiter（本模块公开
  RateLimiter 与 retry_after_estimate），body 在 BodyCapture 内层不可读（裁决）。
- IP 5 req/s 总闸门（覆盖全部接口含探针与未认证流量）已移交 ip_limit.py
  （fix round 1：IP 层运行于 Auth 外层，本模块位于 Auth 内层管不到未认证流量）。

业务维度键（P4-3）：request.state.principal.user_id（Auth 运行于本中间件外层）；
豁免路径无 principal 且 scope=None，不进入业务维度。

实现：内存固定窗口（单实例 MVP；多实例演进时换共享存储，业务逻辑不变——见契约 4.4 定式）。
超限：429 RATE_LIMITED + Retry-After 响应头（秒）。
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.metrics import RATE_LIMIT_HIT_TOTAL
from app.config import Settings
from app.errors import AppError, ErrorCode, http_status

logger = logging.getLogger(__name__)

_EXEMPT_DEVICE_PATHS = {"/healthz", "/readyz", "/metrics"}
_AUTH_PATHS = {"/auth/register", "/v1/auth/register", "/auth/login", "/v1/auth/login"}


class ClockLike(Protocol):
    """可注入时钟（与 infra/clock 一致的 .now() 约定；也接受裸 callable）。"""

    def now(self) -> float: ...


@dataclass
class RateLimiter:
    """固定窗口限流器：`check(key) -> (allowed, retry_after_seconds)`。"""

    limit: int
    window_seconds: int
    clock: Callable[[], float] | ClockLike = field(default=time.monotonic)
    _counts: dict[tuple[int, str], tuple[float, int]] = field(default_factory=dict)

    def _now(self) -> float:
        clock = self.clock
        if callable(clock):
            return clock()
        return clock.now()

    def check(self, key: str) -> tuple[bool, int]:
        now = self._now()
        window_id = int(now // self.window_seconds)
        # 惰性清理已过期窗口，防止无界增长（单设备窗口数有限，清理即足够）
        expired = [(wid, key) for wid, key in self._counts if wid < window_id]
        for window_key in expired:
            del self._counts[window_key]
        entry = self._counts.get((window_id, key))
        if entry is None:
            self._counts[(window_id, key)] = (now, 1)
            return True, 0
        _, count = entry
        if count >= self.limit:
            retry_after = int(self.window_seconds - (now % self.window_seconds)) + 1
            return False, retry_after
        self._counts[(window_id, key)] = (now, count + 1)
        return True, 0

    def retry_after_estimate(self) -> int:
        """当前窗口剩余秒估算（与 check 超限返回值同公式——供 service 层限流后
        handler 设 Retry-After 头；login 用户名桶用）。"""
        now = self._now()
        return int(self.window_seconds - (now % self.window_seconds)) + 1


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self._write_limiter = RateLimiter(
            limit=settings.rate_limit_write_per_minute, window_seconds=60
        )
        self._api_key_limiter = RateLimiter(
            limit=settings.rate_limit_api_key_per_hour, window_seconds=3600
        )
        self._samples_limiter = RateLimiter(
            limit=settings.rate_limit_samples_per_hour, window_seconds=3600
        )
        self._pdf_limiter = RateLimiter(limit=settings.rate_limit_pdf_per_hour, window_seconds=3600)
        self._auth_limiter = RateLimiter(
            limit=settings.rate_limit_auth_per_hour, window_seconds=3600
        )

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        scope = self._scope(request)
        if scope is not None:
            # auth 维度键 = IP；业务维度键 = principal.user_id（运行于 Auth 内层，
            # 认证已设置 request.state.principal——P4-3 切换）；豁免路径无 principal
            # 且 scope=None 不进入本分支
            if scope == "auth":
                key = client_ip
            else:
                principal = getattr(request.state, "principal", None)
                key = principal.user_id if principal is not None else ""
            limiter = {
                "write": self._write_limiter,
                "api_key": self._api_key_limiter,
                "samples": self._samples_limiter,
                "pdf": self._pdf_limiter,
                "auth": self._auth_limiter,
            }[scope]
            allowed, retry_after = limiter.check(key)
            if not allowed:
                return self._rate_limited(request, scope, retry_after)
        return await call_next(request)

    def _scope(self, request: Request) -> str | None:
        """1.6 维度判定：None = 不进入业务维度（IP 总闸门归 ip_limit.py，Auth 外层）。

        fix round 1（契约 1.6 专门维度生效）：实际路由无 /v1 前缀（servers url 承担 /v1），
        专门维度改按无前缀路径匹配，同时兼容带前缀（防御反代剥前缀场景）。
        P4-3：POST /auth/register|/auth/login 增加 auth 维度（键=IP）；login 用户名桶
        在 auth service 内（body 于 BodyCapture 内层，本层不可读——裁决）。
        """
        if request.url.path in _EXEMPT_DEVICE_PATHS:
            return None
        method = request.method
        path = request.url.path
        if method == "POST" and path in _AUTH_PATHS:
            return "auth"
        if method == "POST" and path in ("/samples", "/v1/samples"):
            return "samples"
        if method == "PUT" and path in ("/api-key", "/v1/api-key"):
            return "api_key"
        if method == "POST" and path in ("/pdfs", "/v1/pdfs"):
            return "pdf"
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return "write"
        return None

    def _rate_limited(self, request: Request, scope: str, retry_after: int) -> JSONResponse:
        principal = getattr(request.state, "principal", None)
        logger.warning(
            "rate limited",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "user_id": principal.user_id if principal is not None else "",
                "error_code": "RATE_LIMITED",
                "path": request.url.path,
            },
        )
        # rate_limit_hit_total 指标（structure-contract 8.3；Task 10 接线）
        RATE_LIMIT_HIT_TOTAL.labels(scope=scope).inc()
        response = JSONResponse(
            status_code=http_status(ErrorCode.RATE_LIMITED),
            content=AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试").to_response(),
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
