"""tasks/service.py：任务用例（创建/查询/取消/resume + 状态机 + DB 条件更新）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
创建只落创建快照（PENDING+PLANNING），知识点规划由规划 worker CAS 接管（spec §6.1）。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig
from infra.db.models import ApiKey, Chapter, PdfFile, Task
from infra.db.session import format_utc
from infra.metrics import GENERATION_TASKS_TOTAL
from services.decks.service import _owned as _owned_deck
from services.generation.quota import task_unit_budget
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


def _owned_task(session: Session, *, user_id: str, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None or task.user_id != user_id:
        raise AppError(ErrorCode.TASK_NOT_FOUND, "任务不存在")
    return task


def task_view(task: Task) -> dict[str, object]:
    """任务视图：selected_chapters/generation_config/cursor/sample_cards 从 JSON 反序列化；
    resumable bool；internal_stage 取自 stage 列（V2.5 改名 internal_stage 语义）。"""
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,  # V2.5 归属项目；迁移前历史任务可为 null
        "file_id": task.file_id,
        "deck_id": task.deck_id,
        "retry_of_task_id": task.retry_of_task_id,  # V2.5 失败重试关联
        "status": task.status,
        "internal_stage": task.stage,  # V2.5：stage 列 → internal_stage 语义（运行期观测）
        "selected_chapters": json.loads(task.selected_chapters),
        "generation_config": json.loads(task.generation_config),
        "sample_cards": json.loads(task.sample_cards) if task.sample_cards else None,
        "sample_config_hash": task.sample_config_hash,
        "sample_confirmed_at": task.sample_confirmed_at,
        "cursor": json.loads(task.cursor) if task.cursor else None,
        "generated_card_count": task.generated_card_count,
        "total_batch_count": task.total_batch_count,
        "completed_batch_count": task.completed_batch_count,
        "resumable": bool(task.resumable),
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "completion_reason": task.completion_reason,
        "skipped_planning_group_count": task.skipped_planning_group_count,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "updated_at": task.updated_at,
    }


def create_task(
    session: Session,
    *,
    user_id: str,
    file_id: str,
    deck_id: str,
    chapter_ids: list[str],
    config: GenerationConfig,
    now: str,
    settings: Settings | None = None,
) -> Task:
    """创建任务：校验归属（user 域）/配置/已保存 Key（user 域，无 → API_KEY_NOT_SET 422）→ 预算硬上限
    （spec §10，超限 → VALIDATION_ERROR 不创建）→ 建 Task（PENDING + stage=PLANNING
    + 创建快照 JSON，started_at/total_batch_count 空）；规划由 worker CAS 接管（§6.1），
    本函数不再同事务规划。settings 注入定式同 executor：显式参数 > session.info["settings"]
    > Settings() 环境缺省。
    """
    validate_config(config)
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.user_id != user_id:
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
    _owned_deck(session, user_id=user_id, deck_id=deck_id)
    # 已保存 Key 校验（6.2：无 Key → API_KEY_NOT_SET）；只查 status=AVAILABLE 行存在，
    # 不解密（V5A 生成时才解密调用）。P4-4：按 user 域查询（列投影 Core select——只取 user_id 列）。
    key_row = session.scalar(
        select(ApiKey.user_id).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
    )
    if key_row is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存可用 API Key")
    # 预算硬上限（spec §10）：章节数 × 3 × 密度 > max_generation_units_per_task → 直接拒绝，
    # 不创建任务、不调用 Planner（Planner 合法输出经子配额截断不可能突破该上限）
    if settings is None:
        injected = session.info.get("settings")
        if isinstance(injected, Settings):
            settings = injected
        else:
            settings = Settings()
    budget = task_unit_budget(
        len(chapter_ids), config.coverage_mode
    )  # V2.5：quantity_tendency 改名 coverage_mode
    if budget > settings.max_generation_units_per_task:
        raise AppError(ErrorCode.VALIDATION_ERROR, "生成单元预算超出上限")
    # 创建即 PENDING+PLANNING（spec §6.1）：只落创建快照，started_at/total_batch_count 留空，
    # 规划 worker 首次 CAS 接管时原子刷新快照并转 RUNNING+PLANNING
    task = Task(
        task_id=_uuid4(),
        user_id=user_id,
        file_id=file_id,
        deck_id=deck_id,
        status="PENDING",
        stage="PLANNING",
        selected_chapters=json.dumps(chapter_snapshot, ensure_ascii=False),
        generation_config=json.dumps(config.model_dump(), ensure_ascii=False),
        generated_card_count=0,
        resumable=0,
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    session.flush()
    return task


def get_task(session: Session, *, user_id: str, task_id: str) -> Task:
    return _owned_task(session, user_id=user_id, task_id=task_id)


def cancel_task(session: Session, *, user_id: str, task_id: str, now: str) -> Task:
    """取消：PENDING/RUNNING/PAUSED → CANCELLED；终态任务早返回不转移。

    8.3 generation_tasks_total CANCELLED 只在实际状态转移时计数（不同幂等键重复
    取消不再重复 inc；同键重放走 execute_idempotent 快照，不重跑本函数）。
    """
    task = _owned_task(session, user_id=user_id, task_id=task_id)
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
    user_id: str,
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
    task = _owned_task(session, user_id=user_id, task_id=task_id)
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
