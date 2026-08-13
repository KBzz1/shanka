"""executor.py：任务执行器（4.4 定式：进程内 DB 驱动；V5A adapter 分批执行）。

扫描一轮 = 规划 worker + 生成 worker + 评分 worker（spec §6.1/§8）：

- 规划 worker：先 claim_planning_task（CAS1 首次接管 + 章节快照冻结 / CAS2 孤儿恢复，
  每次扫描至多接管一个 PLANNING 任务）→ run_planning（选页拆组/账本恢复/组调用/合并
  落库，内部短事务提交心跳与终态；LLM 调用在事务外）。
- 生成 worker：扫描 `status='RUNNING' AND stage='GENERATING'` 任务 → 解密 API Key
  （仅 infra/llm 路径）→ 构造带 Key 的 DeepSeekClient（client_factory 注入，测试 mock
  transport）→ 循环 process_next_batch 直至无待处理批次；V5B：每批完成后心跳刷新
  task.updated_at（服务端时钟，孤儿恢复判据）并 commit（批次事务粒度：批次状态+游标+
  心跳同事务落库，长任务中间状态可观测，崩溃后已完成批次保留、未完成批次可恢复）→
  批循环结束 → enter_scoring_stage（条件更新 stage=GENERATING → SCORING）→
  run_scoring_stage（评分回写，内部条件更新 RUNNING+SCORING → COMPLETED）。
- 评分 worker：扫描 `status='RUNNING' AND stage='SCORING'` 任务——心跳超时的孤儿
  （在途 worker 崩溃）经 CAS 条件更新接管 + mark_stale_unknown + 重跑 run_scoring_stage
  （账本为已尝试游标）；心跳新鲜（在途 worker 存活）不干预。
系统级错误（adapter 抛 API_KEY_UNAVAILABLE/GENERATION_FAILED）→ 任务 FAILED（4.1），
failure_stage 按 task.stage 归因（PLANNING/GENERATING/SCORING）；
批次级失败（Schema 重试达上限）→ 批次 SKIPPED，任务继续（4.2）。V4 fake 不再用于任务执行
（样卡仍用 fake）。
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import ApiKey, Batch, KnowledgePoint, Task
from infra.db.session import format_utc
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from infra.metrics import GENERATION_TASKS_DURATION_SECONDS, GENERATION_TASKS_TOTAL
from services.generation.batches import plan_batches, process_next_batch
from services.generation.ledger import mark_stale_unknown
from services.generation.planning_executor import claim_planning_task, run_planning
from services.generation.scoring import enter_scoring_stage, run_scoring_stage

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
    """从 api_keys 表取 encrypted_key 解密（红线 4：仅 infra/llm 路径；明文不落日志/响应）。

    P4-4（原 plan Task 5 前移）：Key 归属切 user 域——按 task.user_id 查询（列投影 Core
    select，只取 encrypted_key 列）；legacy 任务（user_id NULL）无 Key 可解析 →
    API_KEY_UNAVAILABLE（干净 FAILED，不 500）。
    """
    key = key_from_settings(settings)
    if task.user_id is None:  # legacy device 域任务（D-06 无访问路径）
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 不可用（加密配置缺失或未保存 Key）")
    encrypted = session.scalar(
        select(ApiKey.encrypted_key).where(
            ApiKey.user_id == task.user_id, ApiKey.status == "AVAILABLE"
        )
    )
    if key is None or encrypted is None:
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 不可用（加密配置缺失或未保存 Key）")
    try:
        return decrypt_key(encrypted, key)
    except Exception:  # noqa: BLE001 —— 解密失败（畸形 payload/密钥不符）统一 API_KEY_UNAVAILABLE
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 解密失败") from None


def _heartbeat_stale(updated_at: str | None, now: str, timeout_minutes: int) -> bool:
    """心跳超时判据（CAS2 同款）：updated_at 早于 now - timeout → 可判定为孤儿。"""
    if updated_at is None:
        return False
    try:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    return age > timedelta(minutes=timeout_minutes)


def _format_cutoff(now: str, minutes: int) -> str:
    """now - minutes 的 format_utc 字符串（SCORING 孤儿接管 CAS 条件用；字符串比较=时间序）。"""
    try:
        dt = datetime.fromisoformat(now)
    except ValueError:
        return now
    return format_utc(dt - timedelta(minutes=minutes))


def _recover_generating_orphans(session: Session, *, task: Task, settings: Settings) -> None:
    """GENERATING 孤儿恢复（spec §9 恢复语义 + CAS2 同款心跳判据）。

    process_next_batch 在调用前提交"抢占 + STARTED 占位"，进程崩溃会遗留 PROCESSING
    批次与 STARTED 账本行（不随事务回滚）。心跳超时的任务视为孤儿：遗留 STARTED →
    UNKNOWN（仍计入重试预算，防崩溃恢复后突破预算），卡在 PROCESSING 的批次复位
    FAILED（可重新抢占，按账本尝试数续跑，不重复付费）。心跳新鲜（并发 worker 存活）
    时不干预，避免误伤在途批次。
    """
    now = format_utc(SystemClock().now_utc())
    if not _heartbeat_stale(task.updated_at, now, settings.orphan_timeout_minutes):
        return
    marked = mark_stale_unknown(session, task_id=task.task_id, stage="GENERATING", now=now)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Batch)
            .where(Batch.task_id == task.task_id, Batch.status == "PROCESSING")
            .values(status="FAILED")
        ),
    )
    if marked or result.rowcount:
        logger.info(
            "generating orphan recovery",
            extra={
                "task_id": task.task_id,
                "started_to_unknown": marked,
                "processing_to_failed": result.rowcount,
            },
        )


def process_running_tasks(
    session: Session,
    *,
    storage: Any = None,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
) -> int:
    """扫描一轮：先规划 worker（CAS 抢占一个 PLANNING 任务 → run_planning），
    再生成 worker（RUNNING + stage=GENERATING 任务分批生成入库）。返回处理任务数。

    storage 预留：V5A 真实 adapter 读取批次内容时使用（当前批次 prompt 仅用知识点 topic）；
    事务：规划 CAS/快照冻结、每组心跳、最终条件更新由 claim/run_planning 内部短事务
    commit（§4.2 冻结原子性、§6.2 STARTED 占位先行）；_execute_task 每批完成后 commit
    （批次事务粒度：批次状态+游标+心跳同事务落库）；任务终态由调用方最终 commit 落库
    （scan_once / handler）。
    settings/client_factory 测试注入；缺省 settings 取环境配置，client_factory 构造生产客户端。
    """
    settings = settings or Settings()
    now = format_utc(SystemClock().now_utc())
    # 规划 worker 入口（spec §6.1）：每次扫描至多接管一个 PLANNING 任务
    claimed = claim_planning_task(
        session, orphan_timeout_minutes=settings.orphan_timeout_minutes, now=now
    )
    if claimed is not None:
        session.commit()  # CAS1/CAS2 接管 + 章节快照冻结原子提交（§4.2 规划快照冻结时刻）
        try:
            api_key = _decrypt_api_key(session, task=claimed, settings=settings)
            client = (
                client_factory(api_key)
                if client_factory is not None
                else DeepSeekClient(settings, api_key=api_key)
            )
            try:
                run_planning(session, claimed, settings=settings, client=client)
            finally:
                client.close()
        except AppError as exc:
            # 系统级错误（Key 解密失败/上游不可用）→ FAILED + failure_stage=PLANNING（§6.3）
            _fail_task(claimed, error_code=exc.code.value, failure_stage="PLANNING")
            logger.warning(
                "task planning failed",
                extra={"task_id": claimed.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(
                claimed, error_code=ErrorCode.GENERATION_FAILED.value, failure_stage="PLANNING"
            )
            logger.warning("task planning unexpected failure", extra={"task_id": claimed.task_id})
    # 生成 worker 扫描：RUNNING + stage=GENERATING（避免与规划中任务冲突，spec §6.1）
    tasks = session.scalars(
        select(Task)
        .where(Task.status == "RUNNING", Task.stage == "GENERATING")
        .order_by(Task.created_at)
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
    # 评分 worker 扫描：RUNNING + stage=SCORING（spec §8：心跳超时孤儿可 CAS 接管；
    # 心跳新鲜 = 在途 worker 存活，跳过）
    scoring_tasks = session.scalars(
        select(Task)
        .where(Task.status == "RUNNING", Task.stage == "SCORING")
        .order_by(Task.created_at)
    ).all()
    acted = 0
    for task in scoring_tasks:
        try:
            acted += _execute_scoring_task(
                session, task, settings=settings, client_factory=client_factory
            )
        except AppError as exc:
            # 评分 worker 不可恢复错误（资产加载失败等）→ FAILED + failure_stage=SCORING
            _fail_task(task, error_code=exc.code.value)
            logger.warning(
                "task scoring failed",
                extra={"task_id": task.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(task, error_code=ErrorCode.GENERATION_FAILED.value)
            logger.warning("task scoring unexpected failure", extra={"task_id": task.task_id})
    # 处理任务数：同一任务规划 + 生成同轮衔接只计一次；SCORING 孤儿接管按实际行动计数
    claimed_id = claimed.task_id if claimed is not None else None
    return sum(1 for task in tasks if task.task_id != claimed_id) + (1 if claimed else 0) + acted


def _fail_task(task: Task, *, error_code: str, failure_stage: str | None = None) -> None:
    """任务 FAILED 落库；failure_stage 缺省从当前 stage 派生（GENERATING/SCORING
    分支的 worker 错误各自归因——spec 6.4/8：failure_stage 增加 SCORING）。"""
    task.status = "FAILED"
    task.failure_stage = failure_stage or task.stage or "GENERATING"
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
    """执行单个任务：GENERATING 孤儿恢复（心跳超时）→ plan_batches（若未建）→ 解密 Key
    构造 client → 循环 process_next_batch → COMPLETED。"""
    _require_str(task.updated_at, "任务数据不完整（缺少时间戳）")
    _require_str(task.deck_id, "任务数据不完整（缺少牌组）")
    session.info["settings"] = settings  # batches.py 消费（retry_limit/输入字符上限）
    _recover_generating_orphans(session, task=task, settings=settings)
    kps = session.scalars(
        select(KnowledgePoint)
        .where(KnowledgePoint.task_id == task.task_id)
        .order_by(KnowledgePoint.priority)
    ).all()
    if task.total_batch_count is None and kps:
        plan_batches(
            session,
            task_id=task.task_id,
            generation_units=kps,
            now=format_utc(SystemClock().now_utc()),
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
        # V5B 心跳：每批完成后刷新 task.updated_at（服务端时钟 format_utc）并提交——批次事务粒度
        # （批次状态+游标+心跳同事务落库，长任务中间状态可观测，孤儿判据不误判；崩溃后已完成批次
        # 已提交、未完成批次 PENDING/FAILED 可恢复——与 Task 3 崩溃恢复语义一致）
        # final review I-1：每批 commit 后复查任务状态——expire_on_commit=False 下 identity map
        # 停留 RUNNING，批次间隙被 cancel 落库 CANCELLED 后不再抢占下一批（停止处理，保留已入库卡）
        while process_next_batch(session, task_id=task.task_id, client=client) > 0:
            task.updated_at = format_utc(SystemClock().now_utc())
            session.commit()
            session.refresh(task)
            if task.status != "RUNNING":
                break
        # 批循环结束 → SCORING 阶段（spec §8 独立阶段）：条件更新 stage='GENERATING' → 'SCORING'
        # （取消/转移 → rowcount=0 → 不进入评分）；评分 worker 异常上抛 → 调用方 FAILED（failure_stage
        # 由 task.stage=SCORING 派生）
        if enter_scoring_stage(session, task_id=task.task_id, settings=settings):
            session.refresh(task)
            run_scoring_stage(session, task=task, settings=settings, client=client)
            session.refresh(task)
            if task.status == "COMPLETED":
                _observe_task_result(task, "COMPLETED")  # 8.3：任务结果/耗时上报
    finally:
        client.close()


def _execute_scoring_task(
    session: Session,
    task: Task,
    *,
    settings: Settings,
    client_factory: ClientFactory | None,
) -> int:
    """SCORING 孤儿接管（spec §8 + CAS2 同款心跳判据）：仅心跳超时的 RUNNING+SCORING
    任务可被接管（新鲜心跳 = 在途 worker 存活，跳过不干预）；CAS 条件更新接管后
    mark_stale_unknown（遗留 STARTED → UNKNOWN，仍计上限）+ 重跑 run_scoring_stage
    （账本为已尝试游标：已尝试组跳过、未尝试组续跑）。返回实际行动数（1 = 接管，0 = 跳过）。"""
    _require_str(task.updated_at, "任务数据不完整（缺少时间戳）")
    now = format_utc(SystemClock().now_utc())
    if not _heartbeat_stale(task.updated_at, now, settings.orphan_timeout_minutes):
        return 0  # 心跳新鲜 → 在途 worker 存活，不干预
    cutoff = _format_cutoff(now, settings.orphan_timeout_minutes)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task.task_id,
                Task.status == "RUNNING",
                Task.stage == "SCORING",
                Task.updated_at < cutoff,
            )
            .values(updated_at=now)
        ),
    )
    if result.rowcount == 0:
        return 0  # 已被其他 worker 接管或状态已转移
    session.refresh(task)
    marked = mark_stale_unknown(session, task_id=task.task_id, stage="SCORING", now=now)
    if marked:
        logger.info(
            "scoring orphan recovery",
            extra={"task_id": task.task_id, "started_to_unknown": marked},
        )
    session.commit()  # 接管心跳 + 遗留 STARTED→UNKNOWN 原子提交
    api_key = _decrypt_api_key(session, task=task, settings=settings)
    client = (
        client_factory(api_key)
        if client_factory is not None
        else DeepSeekClient(settings, api_key=api_key)
    )
    try:
        run_scoring_stage(session, task=task, settings=settings, client=client)
        session.refresh(task)
        if task.status == "COMPLETED":
            _observe_task_result(task, "COMPLETED")  # 8.3：任务结果/耗时上报
    finally:
        client.close()
    return 1


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
