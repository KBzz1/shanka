"""任务接口（structure-contract 6.4；openapi /tasks）。handler 只做 HTTP 映射。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务（get_db_session 只负责创建/关闭，不提交）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 probes 同理）。
批次列表（6.9/AC-07）：get_task 归属校验 → Batch 视图列表（质量/usage/版本/cost 估算）。
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.tasks import Batch as BatchSchema
from app.schemas.tasks import Task as TaskSchema
from app.schemas.tasks import TaskCreateRequest
from infra.clock import SystemClock
from infra.db.models import Batch
from infra.db.session import format_utc, get_db_session
from services.generation.cost import estimate_cost
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


@router.post("", status_code=201, response_model=TaskSchema)
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


@router.get("/{task_id}", response_model=TaskSchema)
def get_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """任务详情（长任务轮询：状态、stage、已生成数、失败码、是否可继续）。"""
    task = get_task(session, device_id=request.state.device_id, task_id=task_id)
    return JSONResponse(content=task_view(task))


@router.get("/{task_id}/batches", response_model=dict[str, list[BatchSchema]])
def list_task_batches_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """批次列表（契约 6.9/AC-07）：归属校验（404）→ Batch 视图列表（含 cost 估算）。"""
    task = get_task(session, device_id=request.state.device_id, task_id=task_id)
    batches = session.scalars(
        select(Batch).where(Batch.task_id == task.task_id).order_by(Batch.batch_index)
    ).all()
    return JSONResponse(content={"items": [batch_view(b) for b in batches]})


def batch_view(batch: Batch) -> dict[str, object]:
    """Batch 视图：分布/ids JSON 反序列化；cost_estimate 按 8.4 价格常量估算（仅观测，不落库）。"""
    usage = (batch.cache_hit_tokens, batch.cache_miss_tokens, batch.output_tokens)
    has_usage = any(t is not None for t in usage)
    return {
        "batch_id": batch.batch_id,
        "task_id": batch.task_id,
        "batch_index": batch.batch_index,
        "status": batch.status,
        "generated_item_ids": _json_list(batch.generated_item_ids),
        "retry_count": batch.retry_count,
        "coverage_rate": batch.coverage_rate,
        "duplicate_rate": batch.duplicate_rate,
        "difficulty_distribution": _json_dict(batch.difficulty_distribution),
        "chapter_distribution": _json_dict(batch.chapter_distribution),
        "card_type_distribution": _json_dict(batch.card_type_distribution),
        "difficulty_deviation": batch.difficulty_deviation,
        "cache_hit_tokens": batch.cache_hit_tokens,
        "cache_miss_tokens": batch.cache_miss_tokens,
        "output_tokens": batch.output_tokens,
        "request_id": batch.request_id,  # 当前恒 null（R1 live 时透传上游 id）
        "model": batch.model,
        "prompt_version": batch.prompt_version,
        "schema_version": batch.schema_version,
        "rubric_version": batch.rubric_version,
        "duration_ms": batch.duration_ms,
        "http_status": batch.http_status,
        "created_at": batch.created_at,
        "ended_at": batch.ended_at,
        "cost_estimate": (
            estimate_cost(
                cache_hit_tokens=usage[0] or 0,
                cache_miss_tokens=usage[1] or 0,
                output_tokens=usage[2] or 0,
                effective_date=SystemClock().now_utc().date().isoformat(),
            )
            if has_usage
            else None
        ),
    }


def _json_list(raw: str | None) -> list[str] | None:
    """generated_item_ids JSON 字符串 → list；解析失败/缺失 → None（视图不抛错）。"""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _json_dict(raw: str | None) -> dict[str, int] | None:
    """质量分布 JSON 字符串 → dict；解析失败/缺失 → None。"""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


@router.post("/{task_id}/resume", response_model=TaskSchema)
def resume_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    settings: Settings = request.app.state.settings

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = resume_task(
            session,
            device_id=device_id,
            task_id=task_id,
            now=_now(),
            orphan_timeout_minutes=settings.orphan_timeout_minutes,
        )
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


@router.post("/{task_id}/cancel", response_model=TaskSchema)
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
