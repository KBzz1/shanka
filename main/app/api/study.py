"""今日学习计划路由（structure-contract 6.6；openapi /study/today）。handler 只做 HTTP 映射。

GET /study/today 含 get-or-create（偏好首次访问落默认行）——物化写须提交
（依赖 teardown 只 close 不 commit；与 GET /projects/{id}/study-settings 同款）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import execute_idempotent, get_idempotency_key, request_body_hash
from app.schemas.study_plan import StudyPlan, StudyPlanUpdateRequest, TodayStudyPlan
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.study.service import (
    get_study_plan,
    study_plan_backlog,
    today_study_plan,
    update_study_plan,
)

router = APIRouter(tags=["study"])


@router.get("/study/plan", response_model=StudyPlan)
def get_study_plan_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    body = get_study_plan(
        session,
        user_id=request.state.principal.user_id,
        now=format_utc(SystemClock().now_utc()),
    )
    session.commit()
    return JSONResponse(status_code=200, content=body)


@router.put("/study/plan", response_model=StudyPlan)
def put_study_plan_endpoint(
    request: Request,
    payload: StudyPlanUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = "/study/plan"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now = format_utc(SystemClock().now_utc())

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        body = update_study_plan(
            session,
            user_id=user_id,
            project_id=payload.project_id,
            selected_deck_ids=payload.selected_deck_ids,
            daily_new_goal=payload.daily_new_goal,
            daily_review_goal=payload.daily_review_goal,
            now=now,
        )
        return 200, body

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


@router.get("/study/today/backlog")
def get_study_backlog_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    body = study_plan_backlog(
        session,
        user_id=request.state.principal.user_id,
        now=format_utc(SystemClock().now_utc()),
        offset=offset,
        limit=limit,
    )
    session.commit()
    return JSONResponse(status_code=200, content=body)
