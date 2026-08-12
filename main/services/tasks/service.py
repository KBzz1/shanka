"""tasks/service.py：任务用例（创建/查询/取消/resume + 状态机 + DB 条件更新）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
Task 创建与 KnowledgePoint 规划同事务（KnowledgePoint 与 Task 同事务落库）。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig
from infra.db.models import ApiKey, Chapter, PdfFile, Task
from infra.db.session import format_utc
from infra.metrics import GENERATION_TASKS_TOTAL
from services.decks.service import _owned as _owned_deck
from services.generation.planning import plan_knowledge_points
from services.generation.validate import validate_config


def _uuid4() -> str:
    return str(uuid.uuid4())


_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # database-design 0：UTC、恒 3 位毫秒


def _parse_utc(value: str) -> datetime:
    """format_utc 输出（database-design 0 恒 3 位毫秒 Z）→ aware UTC datetime。"""
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def _format_cutoff(now: str, minutes: int) -> str:
    """now - minutes 的 format_utc 字符串（database-design 0 定长格式，字符串比较=时间序）。"""
    return format_utc(_parse_utc(now) - timedelta(minutes=minutes))


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
    config: GenerationConfig,
    now: str,
) -> Task:
    """创建任务：校验归属/配置/已保存 Key（无 → API_KEY_NOT_SET 422）→ 建 Task（RUNNING
    + stage=GENERATING + JSON 快照）→ 规划知识点（同事务）→ 返回 Task 视图。
    """
    validate_config(config)
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    # 章节归属校验：chapter_ids 全部属于 file_id，缺失/他属 → PDF_NOT_FOUND（与 samples 一致）
    chapters = session.scalars(
        select(Chapter).where(Chapter.chapter_id.in_(chapter_ids), Chapter.file_id == file_id)
    ).all()
    by_id = {ch.chapter_id: ch for ch in chapters}
    if any(cid not in by_id for cid in chapter_ids):
        raise AppError(ErrorCode.PDF_NOT_FOUND, "章节不属于该 PDF")
    # selected_chapters 快照存完整 Chapter 对象（契约 3.4 Chapter[]；3.6 章节删除后名称从快照还原）
    chapter_snapshot = [
        {
            "chapter_id": cid,
            "name": by_id[cid].name,
            "start_page": by_id[cid].start_page,
            "end_page": by_id[cid].end_page,
        }
        for cid in chapter_ids
    ]
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
        selected_chapters=json.dumps(chapter_snapshot, ensure_ascii=False),
        generation_config=json.dumps(config.model_dump(), ensure_ascii=False),
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
        quantity_tendency=config.quantity_tendency,
    )
    session.add_all(kps)
    return task


def get_task(session: Session, *, device_id: str, task_id: str) -> Task:
    return _owned_task(session, device_id=device_id, task_id=task_id)


def cancel_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    """取消：PENDING/RUNNING/PAUSED → CANCELLED；终态任务早返回不转移。

    8.3 generation_tasks_total CANCELLED 只在实际状态转移时计数（不同幂等键重复
    取消不再重复 inc；同键重放走 execute_idempotent 快照，不重跑本函数）。
    """
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    if task.status in ("COMPLETED", "FAILED", "CANCELLED"):
        return task  # 已终态：不转移不计数
    task.status = "CANCELLED"
    task.ended_at = now
    task.updated_at = now
    GENERATION_TASKS_TOTAL.labels(result="CANCELLED").inc()
    return task


def resume_task(
    session: Session,
    *,
    device_id: str,
    task_id: str,
    now: str,
    orphan_timeout_minutes: int = 30,
) -> Task:
    """DB 条件更新抢占（4.1）：PAUSED AND resumable=1，或孤儿 RUNNING（心跳超时）→ RUNNING；否则 409。

    orphan_cutoff = now - orphan_timeout_minutes 的 format_utc 字符串（字符串比较=时间序，
    database-design 0 定长格式）；孤儿判据 `RUNNING AND updated_at < cutoff` 由心跳批次
    事务（每批 commit）保证 updated_at 真实反映最后心跳。resume 后 resumable 保持 0
    （RUNNING 继续执行无需再 resume）。SQLAlchemy update 的 rowcount 对 SQLite 有效
    （数据库级行数，非客户端估算）。
    """
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    orphan_cutoff = _format_cutoff(now, orphan_timeout_minutes)
    result: CursorResult[Any] = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task_id,
                ((Task.status == "PAUSED") & (Task.resumable == 1))
                | ((Task.status == "RUNNING") & (Task.updated_at < orphan_cutoff)),
            )
            .values(status="RUNNING", updated_at=now)
        ),
    )
    if result.rowcount == 0:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务不可恢复")
    session.refresh(task)
    return task
