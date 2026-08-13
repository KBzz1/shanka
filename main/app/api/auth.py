"""auth.py：账号路由（structure-contract 6.11；openapi /auth/*；DESIGN §4.4）。

- POST /register、POST /login：豁免 Bearer（中间件豁免清单）与 Idempotency-Key；不走
  execute_idempotent（客户端不得自动重试，防网络重放静默创建多条会话，FR-19）。
- POST /logout：撤销当前会话（principal.session_id），走 execute_idempotent
  （path="/auth/logout"；双头过渡窗口内幂等域仍按 device——Task 3 统一切 user_id）。
- GET /me：返回当前用户最小资料 {"user": AuthUser}；session 撤销/过期 → 401 AUTH_INVALID。
- 明文 token 只出现在 register/login 成功响应（DESIGN §4.3），不进日志。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
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
    user_dict, token, expires_at = login_user(
        session,
        username=payload.username,
        password=payload.password,
        now=now,
        ttl_days=request.app.state.settings.auth_session_ttl_days,
    )
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
        device_id=request.state.device_id,
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
