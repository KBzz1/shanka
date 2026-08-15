"""看板路由（structure-contract 6.6/3.12；openapi /stats/dashboard）。handler 只做 HTTP 映射。

V2.5（Task 11）：无客户端 timezone/weekly_goal 查询参数——服务端按账号学习时区
（preferences.learning_timezone）分桶，周目标 = daily_learning_goal × 7（契约 1.2/3.12）；
V2.4 遗留参数及任意未知查询参数 → 400 VALIDATION_ERROR（统一错误 1.4）。
now 传真实服务端时钟（UTC aware）——看板"当前自然周/streak"以真实日期分桶；
dashboard 内含偏好 get-or-create（物化写，首次访问落默认行）须提交（与 /study/today 同款）。
"""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.schemas.stats import StatsDashboard
from infra.db.session import get_db_session
from services.stats.service import dashboard

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/dashboard", response_model=StatsDashboard)
def dashboard_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    unknown = sorted(set(request.query_params))
    if unknown:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"未知查询参数: {', '.join(unknown)}")
    result = dashboard(
        session,
        user_id=request.state.principal.user_id,
        now=datetime.now(UTC),
    )
    # get-or-create 是物化写（preferences 首次访问落默认行）：须提交（teardown 只 close 不 commit）
    session.commit()
    return JSONResponse(content=result)
