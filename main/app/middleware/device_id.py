"""X-Device-ID 鉴权中间件（structure-contract 1.1；database-design 2.1；红线 3）。

- 缺失/非法设备 ID → 401 DEVICE_ID_REQUIRED / DEVICE_ID_INVALID（1.4 错误响应）。
- 首次见到自动建立 devices 行（first_seen_ip/user_agent/last_active_at）。
- 探针与指标端点（/healthz /readyz /metrics）豁免（8.2/8.3）。
- 校验通过后 request.state.device_id 供后续中间件与 handler 使用。
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.errors import AppError, ErrorCode, http_status
from infra.clock import SystemClock
from infra.db.session import format_utc

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = {"/healthz", "/readyz", "/metrics"}


def _validate_device_id(device_id: str) -> bool:
    try:
        uuid.UUID(device_id)
    except ValueError:
        return False
    return str(uuid.UUID(device_id)) == device_id.lower()


class DeviceIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        device_id = request.headers.get("X-Device-ID")
        if device_id is None:
            return self._error(ErrorCode.DEVICE_ID_REQUIRED, "缺少 X-Device-ID 请求头")
        if not _validate_device_id(device_id):
            return self._error(ErrorCode.DEVICE_ID_INVALID, "X-Device-ID 必须为 UUID v4")
        request.state.device_id = device_id
        await self._register_device(request, device_id)
        return await call_next(request)

    def _error(self, code: ErrorCode, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=http_status(code), content=AppError(code, message).to_response()
        )

    async def _register_device(self, request: Request, device_id: str) -> None:
        """INSERT OR IGNORE devices 行 + 更新 last_active_at（database-design 2.1）。"""
        session_factory = request.app.state.session_factory
        now = format_utc(SystemClock().now_utc())
        first_seen_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("User-Agent") or ""
        try:
            with session_factory() as session:
                session.execute(
                    text(
                        "INSERT OR IGNORE INTO devices (device_id, first_seen_ip, user_agent, last_active_at, created_at) "
                        "VALUES (:device_id, :ip, :ua, :now, :now)"
                    ),
                    {"device_id": device_id, "ip": first_seen_ip, "ua": user_agent, "now": now},
                )
                session.execute(
                    text("UPDATE devices SET last_active_at = :now WHERE device_id = :device_id"),
                    {"device_id": device_id, "now": now},
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "device registration failed",
                extra={
                    "request_id": getattr(request.state, "request_id", ""),
                    "error_code": "INTERNAL_ERROR",
                },
            )
            # 注册失败不阻断请求（数据主体为隐式创建，风控信号可降级）
