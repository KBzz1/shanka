"""任务接口（structure-contract 6.4；openapi /tasks）。handler 只做 HTTP 映射。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务（get_db_session 只负责创建/关闭，不提交）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 probes 同理）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.tasks import TaskCreateRequest
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.tasks.service import (
    cancel_task,
    create_task,
    get_task,
    resume_task,
    task_view,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.post("", status_code=201)
def create_task_endpoint(
    request: Request,
    payload: TaskCreateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = create_task(
            session,
            device_id=device_id,
            file_id=payload.file_id,
            deck_id=payload.deck_id,
            chapter_ids=payload.chapter_ids,
            config=payload.generation_config.model_dump(),
            now=_now(),
        )
        session.flush()
        return 201, task_view(task)

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("/{task_id}")
def get_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """任务详情（长任务轮询：状态、stage、已生成数、失败码、是否可继续）。"""
    task = get_task(session, device_id=request.state.device_id, task_id=task_id)
    return JSONResponse(content=task_view(task))


@router.post("/{task_id}/resume")
def resume_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = resume_task(session, device_id=device_id, task_id=task_id, now=_now())
        return 200, task_view(task)

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/{task_id}/cancel")
def cancel_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = cancel_task(session, device_id=device_id, task_id=task_id, now=_now())
        return 200, task_view(task)

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
