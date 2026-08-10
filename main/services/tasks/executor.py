"""executor.py：任务执行器（4.4 定式：进程内 DB 驱动；V4 用 deterministic fake，V5A 换真实分批）。

V4 同步执行：扫描 RUNNING 任务 → 逐知识点 fake 生成 → 入库（cards + review_state
初始，V1 模式；generation_item_id 部分唯一索引防重——先查后插）→ generated_card_count
更新 → 全部完成 → COMPLETED。fake 失败（不应发生）→ FAILED。
"""

import logging
import uuid
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.errors import AppError, ErrorCode
from infra.db.models import Card, Chapter, KnowledgePoint, ReviewState, Task
from services.generation.fake import generate_card

logger = logging.getLogger(__name__)

# 难度轮换（决策：按 kp priority 轮换三档，BASIC/UNDERSTANDING/APPLICATION；V5A 按 ratio 分配）
_DIFFICULTY_ROTATION = ("BASIC", "UNDERSTANDING", "APPLICATION")


def _require_str(value: str | None, message: str) -> str:
    """任务不变式守卫：RUNNING 任务的 deck/时间戳必有值（创建时写入）。"""
    if value is None:
        raise AppError(ErrorCode.GENERATION_FAILED, message)
    return value


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1


def process_running_tasks(session: Session, *, storage: Any = None) -> int:
    """处理全部 RUNNING 任务（V4 同步执行：逐知识点 fake 生成入库）。返回处理任务数。

    storage 预留：V5A 真实 adapter 读取批次内容时使用；V4 fake 生成不触碰存储。
    事务归调用方（scan_once / handler）：本函数不 commit，失败由调用方回滚。
    """
    tasks = session.scalars(
        select(Task).where(Task.status == "RUNNING").order_by(Task.created_at)
    ).all()
    for task in tasks:
        try:
            _execute_task(session, task)
        except AppError as exc:
            # fake 失败（不应发生）→ FAILED；已入库卡片保留（取消语义同 6.4）
            _fail_task(task, error_code=exc.code.value)
            logger.warning(
                "task execution failed",
                extra={"task_id": task.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(task, error_code=ErrorCode.GENERATION_FAILED.value)
            logger.warning("task execution unexpected failure", extra={"task_id": task.task_id})
    return len(tasks)


def _fail_task(task: Task, *, error_code: str) -> None:
    task.status = "FAILED"
    task.failure_stage = "GENERATING"
    task.error_code = error_code
    task.ended_at = task.updated_at
    task.resumable = 0


def _execute_task(session: Session, task: Task) -> None:
    """执行单个任务：逐知识点 fake 生成 → 入库（防重）→ kp PROCESSED → 任务 COMPLETED。"""
    now = _require_str(task.updated_at, "任务数据不完整（缺少时间戳）")
    deck_id = _require_str(task.deck_id, "任务数据不完整（缺少牌组）")
    kps = session.scalars(
        select(KnowledgePoint)
        .where(KnowledgePoint.task_id == task.task_id)
        .order_by(KnowledgePoint.priority)
    ).all()
    # 章节名映射：fake 的 front/back 展示章节名（kp.topic 已含章节名前缀，直接用）
    chapter_names = {c.chapter_id: c.name for c in session.scalars(select(Chapter)).all()}
    generated = 0
    for kp in kps:
        difficulty = _DIFFICULTY_ROTATION[(kp.priority - 1) % len(_DIFFICULTY_ROTATION)]
        card = generate_card(kp.topic, chapter_names.get(kp.chapter_id or "", ""), difficulty, None)
        # 防重（generation_item_id 部分唯一索引，先查后插；同 seed 已入库则跳过）
        existing = session.scalar(
            select(Card).where(Card.generation_item_id == card["generation_item_id"])
        )
        if existing is not None:
            continue
        c = Card(
            card_id=cast(str, card["card_id"]),
            deck_id=deck_id,
            device_id=task.device_id,
            source="GENERATED",
            position=_next_position(session, deck_id=deck_id),
            front=cast(str, card["front"]),
            back=cast(str, card["back"]),
            card_type=cast(str, card["card_type"]),
            statement=cast("str | None", card.get("statement")),
            answer_boolean=cast("int | None", card.get("answer_boolean")),
            explanation=cast("str | None", card.get("explanation")),
            generation_item_id=cast(str, card["generation_item_id"]),
            target_difficulty=cast(str, card["target_difficulty"]),
            version=cast(str, card["version"]),
            created_at=now,
            updated_at=now,
        )
        session.add(c)
        session.flush()  # 立即暴露 UNIQUE(deck_id, position) / 部分唯一索引冲突
        session.add(
            ReviewState(
                review_state_id=str(uuid.uuid4()),
                card_id=c.card_id,
                state="NEW",
                stability=0.0,
                difficulty=1.0,
                due=now,
                reps=0,
                lapses=0,
                updated_at=now,
            )
        )
        kp.status = "PROCESSED"
        generated += 1
    task.generated_card_count += generated
    task.status = "COMPLETED"
    task.ended_at = now
    task.resumable = 0


def scan_once(session_factory: sessionmaker[Session], *, storage: Any = None) -> int:
    """扫描一轮：处理全部 RUNNING 任务（V3A 同款 session_factory 循环）。返回处理任务数。"""
    with session_factory() as session:
        n = process_running_tasks(session, storage=storage)
        session.commit()
    return n
