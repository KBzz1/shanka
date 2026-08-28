"""任务接口（structure-contract 6.4；openapi /projects/{project_id}/tasks + /tasks）。
handler 只做 HTTP 映射。

V2.5 七态生命周期：POST /projects/{project_id}/tasks（DRAFT 自动保存）→
POST /tasks/{task_id}/samples（SAMPLE_GENERATING，worker 后台完成）→
POST /tasks/{task_id}/start（校验样卡 hash → GENERATING）→ 轮询 GET → 终态；
PATCH 配置变更（样卡失效 → DRAFT）；abandon（正式生成前）；retry（FAILED 关联
新任务）；DELETE（终态任务）。用户侧无 pause/resume/cancel（执行器内部租约恢复）。

写操作幂等接线：handler 内 execute_idempotent(session, ...) → session.commit()，
幂等记录与业务副作用同事务（get_db_session 只负责创建/关闭，不提交）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 probes 同理）。
批次列表（6.9/AC-07）：get_task 归属校验 → Batch 视图列表（质量/usage/版本/cost 估算）。
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
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
from app.schemas.tasks import TaskCreateRequest, TaskUpdateRequest
from infra.clock import SystemClock
from infra.db.models import Batch
from infra.db.session import format_utc, get_db_session
from services.generation.cost import estimate_cost
from services.tasks.service import (
    abandon_task,
    create_task,
    delete_task,
    get_task,
    list_tasks,
    request_samples,
    retry_task,
    start_task,
    task_view,
    update_task,
)

router = APIRouter(tags=["tasks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


def _write(
    session: Session,
    request: Request,
    *,
    path: str,
    biz: Any,
) -> tuple[bool, int, dict[str, Any]]:
    """幂等写接线：execute_idempotent（同事务）→ 返回 (replayed, status, body)。"""
    user_id: str = request.state.principal.user_id
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    return execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=get_idempotency_key(request),
        request_body_hash=body_hash,
        fn=biz,
    )


@router.post("/projects/{project_id}/tasks", status_code=201, response_model=TaskSchema)
def create_task_endpoint(
    request: Request,
    project_id: str,
    payload: TaskCreateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """建立 DRAFT 任务（自动保存语义：保存章节快照、目标牌组和配置，创建即返回）。"""
    user_id: str = request.state.principal.user_id
    settings: Settings = request.app.state.settings

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = create_task(
            session,
            user_id=user_id,
            project_id=project_id,
            deck_id=payload.deck_id,
            chapter_ids=payload.chapter_ids,
            config=payload.generation_config,
            now=_now(),
            settings=settings,  # 预算硬上限（spec §10）
            operation_key=get_idempotency_key(request),
        )
        session.flush()
        return 201, task_view(task)

    _replayed, status, body = _write(
        session, request, path=f"/projects/{project_id}/tasks", biz=biz
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("/tasks", response_model=dict[str, list[TaskSchema]])
def list_tasks_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    project_id: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> JSONResponse:
    """学习页任务区与历史列表（6.4）：user 域 + 可选 project/status 过滤。"""
    tasks = list_tasks(
        session,
        user_id=request.state.principal.user_id,
        project_id=project_id,
        status=status,
    )
    return JSONResponse(content={"items": [task_view(t) for t in tasks]})


@router.get("/tasks/{task_id}", response_model=TaskSchema)
def get_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """任务详情（长任务轮询：七态、internal_stage、样卡、失败码）。"""
    task = get_task(session, user_id=request.state.principal.user_id, task_id=task_id)
    return JSONResponse(content=task_view(task))


@router.patch("/tasks/{task_id}", response_model=TaskSchema)
def update_task_endpoint(
    request: Request,
    task_id: str,
    payload: TaskUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """仅 DRAFT/AWAITING_SAMPLE_CONFIRMATION 可改配置，修改后样卡失效（→ DRAFT）。"""
    user_id: str = request.state.principal.user_id
    updates = payload.model_dump(exclude_unset=True)  # 空 PATCH = 真 no-op

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = update_task(session, user_id=user_id, task_id=task_id, now=_now(), **updates)
        return 200, task_view(task)

    _replayed, status, body = _write(session, request, path=f"/tasks/{task_id}", biz=biz)
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/tasks/{task_id}/samples", response_model=TaskSchema)
def generate_task_samples_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """持久化生成 1~3 张样卡（比例>0 的难度各 1 张；幂等键防重复触发）：
    DRAFT → SAMPLE_GENERATING，worker 后台完成 → AWAITING_SAMPLE_CONFIRMATION。"""
    user_id: str = request.state.principal.user_id

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = request_samples(session, user_id=user_id, task_id=task_id, now=_now())
        return 200, task_view(task)

    _replayed, status, body = _write(session, request, path=f"/tasks/{task_id}/samples", biz=biz)
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/tasks/{task_id}/start", response_model=TaskSchema)
def start_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """校验样卡 hash 后进入 GENERATING（过期样卡 → 409 SAMPLE_STALE）。"""
    user_id: str = request.state.principal.user_id

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = start_task(session, user_id=user_id, task_id=task_id, now=_now())
        return 200, task_view(task)

    _replayed, status, body = _write(session, request, path=f"/tasks/{task_id}/start", biz=biz)
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/tasks/{task_id}/abandon", response_model=TaskSchema)
def abandon_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """放弃任务（只允许正式生成前状态，进入 ABANDONED 终态）。"""
    user_id: str = request.state.principal.user_id

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = abandon_task(session, user_id=user_id, task_id=task_id, now=_now())
        return 200, task_view(task)

    _replayed, status, body = _write(session, request, path=f"/tasks/{task_id}/abandon", biz=biz)
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/tasks/{task_id}/retry", status_code=201, response_model=TaskSchema)
def retry_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """失败任务创建关联新任务（可沿用已确认样卡；retry_of_task_id 指向原任务）。"""
    user_id: str = request.state.principal.user_id
    settings: Settings = request.app.state.settings

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        task = retry_task(
            session,
            user_id=user_id,
            task_id=task_id,
            now=_now(),
            settings=settings,  # 预算硬上限
            operation_key=get_idempotency_key(request),
        )
        session.flush()
        return 201, task_view(task)

    _replayed, status, body = _write(session, request, path=f"/tasks/{task_id}/retry", biz=biz)
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    delete_generated_cards: Annotated[bool, Query()] = False,
) -> Response:
    """删除终态任务历史（delete_generated_cards 选择是否删除其已发布卡）。"""
    user_id: str = request.state.principal.user_id

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_task(
            session,
            user_id=user_id,
            task_id=task_id,
            delete_generated_cards=delete_generated_cards,
        )
        return 204, {}

    _replayed, status, _body = _write(session, request, path=f"/tasks/{task_id}", biz=biz)
    session.commit()
    return Response(status_code=status)


@router.get("/tasks/{task_id}/batches", response_model=dict[str, list[BatchSchema]])
def list_task_batches_endpoint(
    request: Request,
    task_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """批次列表（契约 6.9/AC-07）：归属校验（404）→ Batch 视图列表（含 cost 估算）。"""
    task = get_task(session, user_id=request.state.principal.user_id, task_id=task_id)
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
