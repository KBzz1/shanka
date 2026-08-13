"""auth.py：账号路由（structure-contract 6.11；openapi /auth/*；DESIGN §4.4）。

- POST /register、POST /login：豁免 Bearer（中间件豁免清单）与 Idempotency-Key；不走
  execute_idempotent（客户端不得自动重试，防网络重放静默创建多条会话，FR-19）。
- POST /logout：撤销当前会话（principal.session_id），走 execute_idempotent
  （path="/auth/logout"；P4-3 起幂等域按 user_id）。
- POST /login 用户名桶（P4-3）：service 抛 RATE_LIMITED → 捕获后 429 + Retry-After
  （limiter.retry_after_estimate()）+ rate_limit_hit_total(scope="auth")。
- GET /me：返回当前用户最小资料 {"user": AuthUser}；session 撤销/过期 → 401 AUTH_INVALID。
- 明文 token 只出现在 register/login 成功响应（DESIGN §4.3），不进日志。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.metrics import RATE_LIMIT_HIT_TOTAL
from app.errors import AppError, ErrorCode
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.middleware.rate_limit import RateLimiter
from app.schemas.auth import (
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthSessionResponse,
    AuthUser,
)
from domain.auth import AuthPrincipal
from infra.clock import SystemClock
from infra.db.session import get_db_session
from services.auth.service import (
    get_current_user,
    login_user,
    logout_session,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_response(
    user_dict: dict[str, str], access_token: str, expires_at: str
) -> dict[str, Any]:
    """register(201) 与 login(200) 共用形状（3.15；JSONResponse 直返）。"""
    return AuthSessionResponse(
        user=AuthUser(**user_dict),
        access_token=access_token,
        token_type="Bearer",
        expires_at=expires_at,
    ).model_dump()


@router.post("/register", status_code=201)
def register_endpoint(
    request: Request,
    payload: AuthRegisterRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    now: datetime = SystemClock().now_utc()
    user_dict, token, expires_at = register_user(
        session,
        username=payload.username,
        password=payload.password,
        now=now,
        ttl_days=request.app.state.settings.auth_session_ttl_days,
    )
    session.commit()
    return JSONResponse(status_code=201, content=_session_response(user_dict, token, expires_at))


@router.post("/login", status_code=200)
def login_endpoint(
    request: Request,
    payload: AuthLoginRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    now: datetime = SystemClock().now_utc()
    username_limiter: RateLimiter = request.app.state.login_username_limiter
    try:
        user_dict, token, expires_at = login_user(
            session,
            username=payload.username,
            password=payload.password,
            now=now,
            ttl_days=request.app.state.settings.auth_session_ttl_days,
            username_limiter=username_limiter,
        )
    except AppError as exc:
        if exc.code is ErrorCode.RATE_LIMITED:
            # 用户名桶命中（P4-3）：429 + Retry-After；scope="auth"（structure-contract 8.3）
            RATE_LIMIT_HIT_TOTAL.labels(scope="auth").inc()
            response = JSONResponse(status_code=429, content=exc.to_response())
            response.headers["Retry-After"] = str(username_limiter.retry_after_estimate())
            return response
        raise
    session.commit()
    return JSONResponse(status_code=200, content=_session_response(user_dict, token, expires_at))


@router.post("/logout", status_code=204)
def logout_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    principal: AuthPrincipal = request.state.principal
    key = get_idempotency_key(request)
    path = "/auth/logout"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now: datetime = SystemClock().now_utc()

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        logout_session(session, session_id=principal.session_id, now=now)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        user_id=principal.user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return Response(status_code=status)


@router.get("/me", status_code=200)
def me_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    principal: AuthPrincipal = request.state.principal
    user = get_current_user(session, session_id=principal.session_id)
    return JSONResponse(status_code=200, content={"user": AuthUser(**user).model_dump()})
