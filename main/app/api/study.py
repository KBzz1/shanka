"""今日学习计划路由（structure-contract 6.6；openapi /study/today）。handler 只做 HTTP 映射。

GET /study/today 含 get-or-create（偏好首次访问落默认行）——物化写须提交
（依赖 teardown 只 close 不 commit；与 GET /projects/{id}/study-settings 同款）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.schemas.study_plan import TodayStudyPlan
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.study.service import today_study_plan

router = APIRouter(tags=["study"])


@router.get("/study/today", response_model=TodayStudyPlan)
def get_today_study_plan_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    now = format_utc(SystemClock().now_utc())
    body = today_study_plan(session, user_id=request.state.principal.user_id, now=now)
    # get-or-create 是物化写（preferences 首次访问落默认行）：须提交（teardown 只 close 不 commit）
    session.commit()
    return JSONResponse(content=body)
