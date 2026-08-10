"""限流中间件（structure-contract 1.6；红线 3）。

维度判定（1.6 表）：
- IP 5 req/s：全部接口（含探针，采集器例外由部署层处理——契约字面"全部接口"）。
- 写操作 60 req/min/device：全部写接口（POST/PUT/PATCH/DELETE），
  被专门维度覆盖的接口（/api-key、/samples、/pdfs）除外。
- PUT /api-key 10 次/时/device；POST /samples 20 次/时/device；POST /pdfs 10 次/时/device。

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

from app.config import Settings
from app.errors import AppError, ErrorCode, http_status

logger = logging.getLogger(__name__)

_EXEMPT_DEVICE_PATHS = {"/healthz", "/readyz", "/metrics"}


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


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self._ip_limiter = RateLimiter(limit=settings.rate_limit_ip_per_second, window_seconds=1)
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

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = self._ip_limiter.check(client_ip)
        if not allowed:
            return self._rate_limited(request, "ip", retry_after)
        scope = self._scope(request)
        if scope is not None:
            # 运行于 DeviceID 外层（裁决顺序 Metrics → RequestID → RateLimit → DeviceID →
            # Logging），request.state.device_id 尚未设置，键用原始请求头
            device_id = request.headers.get("X-Device-ID") or ""
            limiter = {
                "write": self._write_limiter,
                "api_key": self._api_key_limiter,
                "samples": self._samples_limiter,
                "pdf": self._pdf_limiter,
            }[scope]
            allowed, retry_after = limiter.check(device_id)
            if not allowed:
                return self._rate_limited(request, scope, retry_after)
        return await call_next(request)

    def _scope(self, request: Request) -> str | None:
        """1.6 维度判定：None = 仅 IP 维度。"""
        if request.url.path in _EXEMPT_DEVICE_PATHS:
            return None
        method = request.method
        path = request.url.path
        if method == "POST" and path == "/v1/samples":
            return "samples"
        if method == "PUT" and path == "/v1/api-key":
            return "api_key"
        if method == "POST" and path == "/v1/pdfs":
            return "pdf"
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return "write"
        return None

    def _rate_limited(self, request: Request, scope: str, retry_after: int) -> JSONResponse:
        logger.warning(
            "rate limited",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "device_id": getattr(request.state, "device_id", ""),
                "error_code": "RATE_LIMITED",
                "path": request.url.path,
            },
        )
        # rate_limit_hit_total 指标（Task 10 接线；metrics.py 创建后移除 type: ignore）
        try:
            from app.api.metrics import RATE_LIMIT_HIT_TOTAL  # type: ignore[import-untyped]

            RATE_LIMIT_HIT_TOTAL.labels(scope=scope).inc()
        except ImportError:
            pass
        response = JSONResponse(
            status_code=http_status(ErrorCode.RATE_LIMITED),
            content=AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试").to_response(),
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
