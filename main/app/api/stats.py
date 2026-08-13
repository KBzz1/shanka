"""看板路由（structure-contract 6.8；openapi /stats/dashboard）。handler 只做 HTTP 映射。

now 传真实服务端时钟（UTC aware）——看板"当前自然周/streak"以真实日期分桶；
weekly_goal ge=1 校验（openapi minimum 1）→ FastAPI 422 → 统一错误包装 400 VALIDATION_ERROR；
非法 timezone 由 service 抛 VALIDATION_ERROR 400。
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.schemas.stats import StatsDashboard
from infra.db.session import get_db_session
from services.stats.service import dashboard

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/dashboard", response_model=StatsDashboard)
def dashboard_endpoint(
    request: Request,
    timezone: Annotated[str, Query(description="IANA 时区名称")],
    session: Annotated[Session, Depends(get_db_session)],
    weekly_goal: Annotated[int | None, Query(ge=1)] = None,
) -> JSONResponse:
    result = dashboard(
        session,
        user_id=request.state.principal.user_id,
        timezone=timezone,
        weekly_goal=weekly_goal,
        now=datetime.now(UTC),
    )
    return JSONResponse(content=result)
