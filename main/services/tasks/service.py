"""tasks/service.py：任务用例（创建/查询/取消/resume + 状态机 + DB 条件更新）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
Task 创建与 KnowledgePoint 规划同事务（KnowledgePoint 与 Task 同事务落库）。
"""

import json
import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, PdfFile, Task
from services.decks.service import _owned as _owned_deck
from services.generation.planning import plan_knowledge_points
from services.generation.validate import validate_config


def _uuid4() -> str:
    return str(uuid.uuid4())


def _owned_task(session: Session, *, device_id: str, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.device_id != device_id:
        raise AppError(ErrorCode.TASK_NOT_FOUND, "任务不存在")
    return task


def task_view(task: Task) -> dict[str, object]:
    """任务视图：selected_chapters/generation_config/cursor 从 JSON 反序列化；resumable bool。"""
    return {
        "task_id": task.task_id,
        "file_id": task.file_id,
        "deck_id": task.deck_id,
        "status": task.status,
        "stage": task.stage,
        "selected_chapters": json.loads(task.selected_chapters),
        "generation_config": json.loads(task.generation_config),
        "cursor": json.loads(task.cursor) if task.cursor else None,
        "generated_card_count": task.generated_card_count,
        "total_batch_count": task.total_batch_count,
        "completed_batch_count": task.completed_batch_count,
        "resumable": bool(task.resumable),
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "updated_at": task.updated_at,
    }


def create_task(
    session: Session,
    *,
    device_id: str,
    file_id: str,
    deck_id: str,
    chapter_ids: list[str],
    config: dict[str, Any],
    now: str,
) -> Task:
    """创建任务：校验归属/配置/已保存 Key（无 → API_KEY_NOT_SET 422）→ 建 Task（RUNNING
    + stage=GENERATING + JSON 快照）→ 规划知识点（同事务）→ 返回 Task 视图。
    """
    validate_config(config)
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    _owned_deck(session, device_id=device_id, deck_id=deck_id)
    # 已保存 Key 校验（6.2：无 Key → API_KEY_NOT_SET）；只查 status=AVAILABLE 行存在，
    # 不解密（V5A 生成时才解密调用）
    key_row = session.scalar(
        select(ApiKey).where(ApiKey.device_id == device_id, ApiKey.status == "AVAILABLE")
    )
    if key_row is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存可用 API Key")
    # 决策：创建即 RUNNING（后台循环立即处理）；规划同步完成（stage 直接 GENERATING，异步生成）
    task = Task(
        task_id=_uuid4(),
        device_id=device_id,
        file_id=file_id,
        deck_id=deck_id,
        status="RUNNING",
        stage="GENERATING",
        selected_chapters=json.dumps(chapter_ids, ensure_ascii=False),
        generation_config=json.dumps(config, ensure_ascii=False),
        generated_card_count=0,
        resumable=0,
        created_at=now,
        started_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    kps = plan_knowledge_points(
        session,
        task_id=task.task_id,
        chapter_ids=chapter_ids,
        quantity_tendency=config["quantity_tendency"],
    )
    session.add_all(kps)
    return task


def get_task(session: Session, *, device_id: str, task_id: str) -> Task:
    return _owned_task(session, device_id=device_id, task_id=task_id)


def cancel_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    """取消：PENDING/RUNNING/PAUSED → CANCELLED（终态任务保持不变）。"""
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    if task.status in ("PENDING", "RUNNING", "PAUSED"):
        task.status = "CANCELLED"
        task.ended_at = now
        task.updated_at = now
    return task


def resume_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    """DB 条件更新抢占（4.1）：PAUSED AND resumable=1 → RUNNING；否则 409 TASK_STATE_CONFLICT。

    SQLAlchemy update 的 rowcount 对 SQLite 有效（数据库级行数，非客户端估算）。
    """
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    result: CursorResult[Any] = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(Task.task_id == task_id, Task.status == "PAUSED", Task.resumable == 1)
            .values(status="RUNNING", updated_at=now)
        ),
    )
    if result.rowcount == 0:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务不可恢复")
    session.refresh(task)
    return task
