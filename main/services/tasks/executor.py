"""executor.py：任务执行器（4.4 定式：进程内 DB 驱动；V5A adapter 分批执行）。

V5A 同步执行：扫描 RUNNING 任务 → plan_batches（若任务无批次）→ 解密 API Key（仅 infra/llm
路径）→ 构造带 Key 的 DeepSeekClient（client_factory 注入，测试 mock transport）→ 循环
process_next_batch 直至无待处理批次（每批完成后心跳刷新 task.updated_at，服务端时钟——
V5B 孤儿恢复判据）→ 全部批次终态 → 任务 COMPLETED。
系统级错误（adapter 抛 API_KEY_UNAVAILABLE/GENERATION_FAILED）→ 任务 FAILED（4.1）；
批次级失败（Schema 重试达上限）→ 批次 SKIPPED，任务继续（4.2）。V4 fake 不再用于任务执行
（样卡仍用 fake）。
"""

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import ApiKey, KnowledgePoint, Task
from infra.db.session import format_utc
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from infra.metrics import GENERATION_TASKS_DURATION_SECONDS, GENERATION_TASKS_TOTAL
from services.generation.batches import plan_batches, process_next_batch

logger = logging.getLogger(__name__)

# client_factory(decrypted_api_key) -> DeepSeekClient：测试注入 mock transport；生产缺省
ClientFactory = Callable[[str], DeepSeekClient]


def _require_str(value: str | None, message: str) -> str:
    """任务不变式守卫：RUNNING 任务的 deck/时间戳必有值（创建时写入）。"""
    if value is None:
        raise AppError(ErrorCode.GENERATION_FAILED, message)
    return value


def _observe_task_result(task: Task, result: str) -> None:
    """8.3 generation_tasks_total(result) + generation_tasks_duration_seconds（started_at→ended_at）。"""
    GENERATION_TASKS_TOTAL.labels(result=result).inc()
    seconds = _duration_seconds(task.started_at, task.ended_at)
    if seconds is not None:
        GENERATION_TASKS_DURATION_SECONDS.observe(seconds)


def _duration_seconds(start: str | None, end: str | None) -> float | None:
    """UTC ISO 字符串（format_utc 格式）耗时秒数；解析失败/缺失 → None（不观测）。"""
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(delta.total_seconds(), 0.0)


def _decrypt_api_key(session: Session, *, task: Task, settings: Settings) -> str:
    """从 api_keys 表取 encrypted_key 解密（红线 4：仅 infra/llm 路径；明文不落日志/响应）。"""
    key = key_from_settings(settings)
    row = session.scalar(
        select(ApiKey).where(ApiKey.device_id == task.device_id, ApiKey.status == "AVAILABLE")
    )
    if key is None or row is None:
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 不可用（加密配置缺失或未保存 Key）")
    try:
        return decrypt_key(row.encrypted_key, key)
    except Exception:  # noqa: BLE001 —— 解密失败（畸形 payload/密钥不符）统一 API_KEY_UNAVAILABLE
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 解密失败") from None


def process_running_tasks(
    session: Session,
    *,
    storage: Any = None,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
) -> int:
    """处理全部 RUNNING 任务（V5A adapter 分批生成入库）。返回处理任务数。

    storage 预留：V5A 真实 adapter 读取批次内容时使用（当前批次 prompt 仅用知识点 topic）；
    事务归调用方（scan_once / handler）：本函数不 commit，失败由调用方回滚。
    settings/client_factory 测试注入；缺省 settings 取环境配置，client_factory 构造生产客户端。
    """
    settings = settings or Settings()
    tasks = session.scalars(
        select(Task).where(Task.status == "RUNNING").order_by(Task.created_at)
    ).all()
    for task in tasks:
        try:
            _execute_task(session, task, settings=settings, client_factory=client_factory)
        except AppError as exc:
            # 系统级错误（API Key 失效/上游持续不可用）→ FAILED；已入库卡片保留（4.1）
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
    _observe_task_result(task, "FAILED")  # 8.3：系统级失败也计数


def _execute_task(
    session: Session,
    task: Task,
    *,
    settings: Settings,
    client_factory: ClientFactory | None,
) -> None:
    """执行单个任务：plan_batches（若未建）→ 解密 Key 构造 client → 循环 process_next_batch → COMPLETED。"""
    now = _require_str(task.updated_at, "任务数据不完整（缺少时间戳）")
    _require_str(task.deck_id, "任务数据不完整（缺少牌组）")
    session.info["settings"] = settings  # batches.py 消费（batch_size/retry_limit）
    kps = session.scalars(
        select(KnowledgePoint)
        .where(KnowledgePoint.task_id == task.task_id)
        .order_by(KnowledgePoint.priority)
    ).all()
    if task.total_batch_count is None and kps:
        plan_batches(
            session,
            task_id=task.task_id,
            knowledge_points=kps,
            batch_size=settings.batch_size,
        )
        session.flush()
    api_key = _decrypt_api_key(session, task=task, settings=settings)
    client = (
        client_factory(api_key)
        if client_factory is not None
        else DeepSeekClient(settings, api_key=api_key)
    )
    try:
        # 批次级失败（Schema 重试达上限 → SKIPPED）不中断；adapter 系统错误抛 AppError 上抛
        # V5B 心跳：每批完成后刷新 task.updated_at（服务端时钟 format_utc）——孤儿恢复判据（Task 2）
        while process_next_batch(session, task_id=task.task_id, client=client) > 0:
            task.updated_at = format_utc(SystemClock().now_utc())
    finally:
        client.close()
    task.status = "COMPLETED"
    task.ended_at = now
    task.resumable = 0
    _observe_task_result(task, "COMPLETED")  # 8.3：任务结果/耗时上报


def scan_once(
    session_factory: sessionmaker[Session],
    *,
    storage: Any = None,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
) -> int:
    """扫描一轮：处理全部 RUNNING 任务（V3A 同款 session_factory 循环）。返回处理任务数。"""
    with session_factory() as session:
        n = process_running_tasks(
            session,
            storage=storage,
            settings=settings,
            client_factory=client_factory,
        )
        session.commit()
    return n
