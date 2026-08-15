"""preferences.py：账号偏好路由（structure-contract 6.1；openapi /preferences；V2.5 新增）。

- GET /preferences：get-or-create 默认行（BALANCED / 40/40/20 / 50 / Asia/Shanghai / 无当前项目）。
- PATCH /preferences：部分更新 last-success-wins（model_dump(exclude_unset=True)：未提及字段
  保持原值，显式 null 清空 current_project_id）；比例/每日目标非法 → 400 INVALID_PREFERENCES
  （服务层校验，不得被 pydantic 拒绝成 VALIDATION_ERROR）；IANA 时区非法 →
  400 INVALID_LEARNING_TIMEZONE；不存在的项目 → 404 PROJECT_NOT_FOUND；
  写操作强制 Idempotency-Key 并走 execute_idempotent。
- API-key 字段不进入本资源载荷（6.1）；无 ORM 对象外泄（返回 schema 化 dict）。
"""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.preferences import UserPreferencesUpdateRequest
from infra.clock import SystemClock
from infra.db.session import get_db_session
from services.preferences.service import get_preferences, update_preferences

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("", status_code=200)
def get_preferences_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    now: datetime = SystemClock().now_utc()
    body = get_preferences(session, user_id=request.state.principal.user_id, now=now)
    # get-or-create 是物化写：首次访问落默认值行须提交（依赖 teardown 只 close 不 commit）
    session.commit()
    return JSONResponse(status_code=200, content=body)


@router.patch("", status_code=200)
def patch_preferences_endpoint(
    request: Request,
    payload: UserPreferencesUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = "/preferences"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now: datetime = SystemClock().now_utc()

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        # exclude_unset：仅序列化显式提供的字段——未提及的字段（含 current_project_id）
        # 保持原值（last-success-wins），显式 null 仍保留清空语义（R1 审查 I-1）
        result = update_preferences(
            session, user_id=user_id, payload=payload.model_dump(exclude_unset=True), now=now
        )
        return 200, result

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
