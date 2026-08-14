"""Bearer 认证中间件（structure-contract 1.1 V2.2；DESIGN §4.3；红线 3）。

- 豁免：探针/指标/接口文档（/healthz /readyz /metrics /openapi.json）与 /auth/register、
  /auth/login（6.11：无鉴权端点）；其余业务接口全部需要 Bearer。
- Authorization 头 `Bearer <token>` 解析：缺失/空 → 401 AUTH_REQUIRED；非 Bearer 前缀
  或 token 未知/撤销/过期 → 401 AUTH_INVALID；两者均携带 `WWW-Authenticate: Bearer`。
- 通过后 request.state.principal = AuthPrincipal(user_id, session_id) 供后续中间件与
  handler 使用。
- V2.4 滑动续期：resolve 成功后 renew_session_if_due 按天节流延长 expires_at（活跃永不过期）。
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.errors import AppError, ErrorCode, http_status
from infra.clock import SystemClock
from infra.db.session import format_utc
from services.auth.service import renew_session_if_due, resolve_principal
from services.auth.tokens import hash_session_token

_AUTH_EXEMPT_PATHS = {
    "/healthz",
    "/readyz",
    "/metrics",
    "/openapi.json",
    "/auth/register",
    "/auth/login",
}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)
        header = request.headers.get("Authorization")
        if header is None or not header.strip():
            return self._error(ErrorCode.AUTH_REQUIRED, "缺少 Bearer 凭证")
        if not header.startswith("Bearer "):
            return self._error(ErrorCode.AUTH_INVALID, "Authorization 必须为 Bearer 凭证")
        token = header[len("Bearer ") :].strip()
        if not token:
            return self._error(ErrorCode.AUTH_REQUIRED, "缺少 Bearer 凭证")
        token_hash = hash_session_token(token)
        now_utc = SystemClock().now_utc()
        now = format_utc(now_utc)
        with request.app.state.session_factory() as session:
            principal = resolve_principal(session, token_hash=token_hash, now=now)
            if principal is None:
                return self._error(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")
            renew_session_if_due(
                session,
                session_id=principal.session_id,
                now=now_utc,
                ttl_days=request.app.state.settings.auth_session_ttl_days,
            )
            session.commit()
        request.state.principal = principal
        return await call_next(request)

    def _error(self, code: ErrorCode, message: str) -> JSONResponse:
        response = JSONResponse(
            status_code=http_status(code), content=AppError(code, message).to_response()
        )
        # 契约 1.4/6.11：受保护接口 401 一律携带 WWW-Authenticate: Bearer
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
