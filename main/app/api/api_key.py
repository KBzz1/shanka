"""api_key.py：Key 保存/状态路由（structure-contract 6.2；openapi /api-key）。

- PUT /api-key：校验（validate_key）+ 仅 AVAILABLE 落库；幂等（同 key 同 body 重放，重放不重校验）；
  DeepSeekClient 请求级构造，try/finally close（biz 抛异常也释放）；加密密钥缺失 → 500 配置错误。
- GET /api-key/status：只读 DB 状态（不解密不重校验）；未保存 → UNKNOWN + masked_key 空串。
- 明文 Key 只在 handler → service → adapter 调用栈内（红线 4），响应仅返回状态与脱敏标识。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.api_key import ApiKeyPutRequest
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from infra.llm.crypto import key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.api_key.service import get_status, save_key

router = APIRouter(prefix="/api-key", tags=["api-key"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


def _require_encryption_key(settings: Settings) -> bytes:
    key = key_from_settings(settings)
    if key is None:
        raise AppError(ErrorCode.INTERNAL_ERROR, "API Key 加密密钥未配置")
    return key


@router.put("")
def save_api_key_endpoint(
    request: Request,
    payload: ApiKeyPutRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = "/api-key"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    settings: Settings = request.app.state.settings
    encryption_key = _require_encryption_key(settings)
    client = DeepSeekClient(settings)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        result = save_key(
            session,
            device_id=device_id,
            api_key=payload.api_key,
            encryption_key=encryption_key,
            client=client,
            now=_now(),
        )
        return 200, result

    try:
        _replayed, status, body = execute_idempotent(
            session,
            device_id=device_id,
            path=path,
            idempotency_key=key,
            request_body_hash=body_hash,
            fn=biz,
        )
        session.commit()
    finally:
        # 请求级 client：无论 biz/幂等/提交成功与否都释放（httpx 连接池）
        client.close()
    return JSONResponse(status_code=status, content=body)


@router.get("/status")
def api_key_status_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    # get_status 不解密（仅返回 DB 状态）；加密密钥缺失时传空 bytes 无碍
    encryption_key = key_from_settings(settings) or b""
    result = get_status(session, device_id=request.state.device_id, encryption_key=encryption_key)
    return JSONResponse(content=result)
