"""今日学习计划路由（structure-contract 6.6；openapi /study/today、/study/sessions）。handler 只做 HTTP 映射。

GET /study/today 含 get-or-create（偏好首次访问落默认行）——物化写须提交
（依赖 teardown 只 close 不 commit；与 GET /projects/{id}/study-settings 同款）。
V25-D-37：/study/sessions 三端点（begin/report/list）；begin/report 为幂等写
（Idempotency-Key + execute_idempotent，PUT /study/plan 同款）；report 的绝对值
max 合并天然幂等，键层重放仅作快照一致性兜底。
"""

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.middleware.idempotency import execute_idempotent, get_idempotency_key, request_body_hash
from app.schemas.study_plan import StudyPlan, StudyPlanUpdateRequest, TodayStudyPlan
from app.schemas.study_session import (
    StudySession,
    StudySessionBeginRequest,
    StudySessionBeginResponse,
    StudySessionReportRequest,
)
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.study.service import (
    get_study_plan,
    study_plan_backlog,
    today_study_plan,
    update_study_plan,
)
from services.study.sessions import (
    begin_or_resume,
    report_study_session,
    reset_review_queue,
    sessions_of_day,
    sessions_summary,
)

router = APIRouter(tags=["study"])

_STUDY_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


@router.post("/study/sessions", response_model=StudySessionBeginResponse)
def post_study_session_endpoint(
    request: Request,
    payload: StudySessionBeginRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = "/study/sessions"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now = format_utc(SystemClock().now_utc())

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        body = begin_or_resume(
            session,
            user_id=user_id,
            origin=payload.origin,
            deck_id=payload.deck_id,
            now=now,
            reset=payload.reset,
        )
        if payload.reset:
            if payload.deck_id is None:
                raise AppError(ErrorCode.VALIDATION_ERROR, "重置须针对单一卡组会话")
            # 重置 = 封存旧会话 + 从 0 计时的新会话 + 全卡组复盘队列（新卡最前、
            # 其余按 FSRS 遗忘风险降序；不改写任何评分事实）。
            body = {
                **body,
                "cards": reset_review_queue(
                    session, user_id=user_id, deck_id=payload.deck_id, now=now
                ),
            }
        return 200, body

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    # begin 的 get-or-create（偏好默认行）与会话行都是物化写：须提交
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.patch("/study/sessions/{session_id}", response_model=StudySession)
def patch_study_session_endpoint(
    request: Request,
    session_id: str,
    payload: StudySessionReportRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/study/sessions/{session_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now = format_utc(SystemClock().now_utc())

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        body = report_study_session(
            session,
            user_id=user_id,
            session_id=session_id,
            study_seconds=payload.study_seconds,
            ended=payload.ended,
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


@router.get("/study/sessions")
def get_study_sessions_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    study_date: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    if study_date is not None:
        if not _STUDY_DATE_PATTERN.match(study_date):
            raise AppError(ErrorCode.VALIDATION_ERROR, "study_date 须为 yyyy-MM-dd")
        try:
            date.fromisoformat(study_date)
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION_ERROR, "study_date 非法日期") from exc
    body = sessions_of_day(
        session,
        user_id=request.state.principal.user_id,
        now=format_utc(SystemClock().now_utc()),
        study_date=study_date,
    )
    # 缺省学习日的 get-or-create（偏好默认行）是物化写：须提交
    session.commit()
    return JSONResponse(status_code=200, content=body)


@router.get("/study/sessions/summary")
def get_study_sessions_summary_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    body = sessions_summary(session, user_id=request.state.principal.user_id)
    return JSONResponse(status_code=200, content=body)
