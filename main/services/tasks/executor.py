"""executor.py：任务执行器（4.4 定式：进程内 DB 驱动；V5A adapter 分批执行）。

扫描一轮 = 样卡 worker + 规划 worker + 生成 worker + 评分 worker（V2.5 七态）：

- 样卡 worker：扫描 `status='SAMPLE_GENERATING'` 任务 → 按任务配置生成 1~3 张样卡
  （比例>0 的难度各 1 张，fake 确定性生成，不入库）→ 条件更新持久化样卡 + 配置
  指纹 → `AWAITING_SAMPLE_CONFIRMATION`（WHERE SAMPLE_GENERATING：并发 abandon 后
  rowcount=0 不写入，后台写入无害）。不可恢复错误 → FAILED（可重试/删除，不悬挂）。
- 规划 worker：先 claim_planning_task（CAS1 首次接管 + 章节快照冻结 / CAS2 孤儿恢复，
  每次扫描至多接管一个 PLANNING 任务；V2.5 全程用户状态 GENERATING，接管只动
  internal_stage/心跳）→ run_planning（选页拆组/账本恢复/组调用/合并落库，内部短
  事务提交心跳与终态；LLM 调用在事务外）。
- 生成 worker：扫描 `status='GENERATING' AND stage='GENERATING'` 任务 → 解密 API Key
  （仅 infra/llm 路径）→ 构造带 Key 的 DeepSeekClient（client_factory 注入，测试 mock
  transport）→ 循环 process_next_batch 直至无待处理批次；V5B：每批完成后心跳刷新
  task.updated_at（服务端时钟，孤儿恢复判据）并 commit（批次事务粒度：批次状态+游标+
  心跳同事务落库，长任务中间状态可观测，崩溃后已完成批次保留、未完成批次可恢复）→
  批循环结束 → enter_scoring_stage（条件更新 stage=GENERATING → SCORING）→
  run_scoring_stage（评分回写，内部条件更新 GENERATING+SCORING → COMPLETED）。
- 评分 worker：扫描 `status='GENERATING' AND stage IN ('SCORING','PUBLISHING')` 任务——
  心跳超时的孤儿（在途 worker 崩溃）经 CAS 条件更新接管：SCORING 孤儿
  mark_stale_unknown + 重跑 run_scoring_stage（账本为已尝试游标）；PUBLISHING 孤儿
  （崩溃于 SCORING→PUBLISHING 提交与发布之间）直接执行原子发布（无 LLM）。心跳新鲜
  （在途 worker 存活）不干预。
- 原子发布（4.1）：任务进入 PUBLISHING 后，同一短事务内 CAS 门卫（WHERE
  GENERATING+PUBLISHING）→ 校验 ≥1 张 STAGED 卡 → 全部置 PUBLISHED → 任务
  COMPLETED + generated_card_count（只统计已发布卡，3.4）；0 张有效卡 → 任务
  FAILED + TASK_ZERO_CARDS（V25-D-23）。任何阶段失败 → FAILED，STAGED 卡继续
  隔离，用户侧零部分可见。
系统级错误（adapter 抛 API_KEY_UNAVAILABLE/GENERATION_FAILED）→ 任务 FAILED（4.1），
failure_stage 按 task.stage 归因（PLANNING/GENERATING/SCORING/PUBLISHING）；
批次级失败（Schema 重试达上限）→ 批次 SKIPPED，任务继续（4.2）。V4 fake 不再用于任务执行
（样卡仍用 fake）。V2.5：无 PAUSED/resume/cancel——内部恢复经租约/心跳，不暴露用户状态。
"""

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig
from infra.clock import SystemClock
from infra.db.models import ApiKey, Batch, Card, KnowledgePoint, Task
from infra.db.session import format_utc
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient, LlmChatClient
from infra.metrics import GENERATION_TASKS_DURATION_SECONDS, GENERATION_TASKS_TOTAL
from services.generation.batches import plan_batches, process_next_batch
from services.generation.ledger import mark_stale_unknown
from services.generation.planning_executor import claim_planning_task, run_planning
from services.generation.samples import config_fingerprint, sample_cards_llm
from services.generation.scoring import enter_scoring_stage, run_scoring_stage
from services.tasks.lease import (
    TaskLease,
    claim_task,
    recover_expired_leases,
    release_task,
    renew_task,
    require_lease,
)
from services.tasks.operations import finish_operation
from services.tasks.service import complete_samples

logger = logging.getLogger(__name__)

# client_factory(decrypted_api_key) -> LlmChatClient：生产缺省构造 DeepSeekClient；测试注入 stub
ClientFactory = Callable[[str], LlmChatClient]


def _require_str(value: str | None, message: str) -> str:
    """任务不变式守卫：GENERATING 任务的 deck/时间戳必有值（创建时写入）。"""
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
        seconds = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(seconds.total_seconds(), 0.0)


def _decrypt_api_key(session: Session, *, task: Task, settings: Settings) -> str:
    """从 api_keys 表取 encrypted_key 解密（红线 4：仅 infra/llm 路径；明文不落日志/响应）。

    P4-4（原 plan Task 5 前移）：Key 归属切 user 域——按 task.user_id 查询（列投影 Core
    select，只取 encrypted_key 列）；legacy 任务（user_id NULL）无 Key 可解析 →
    API_KEY_UNAVAILABLE（干净 FAILED，不 500）。
    """
    key = key_from_settings(settings)
    if (
        task.user_id is None
    ):  # 防御：user_id 缺失的历史行（V2.3 起旧 device 域行已删除，防御分支保留）
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


def _publish_guard_update(
    session: Session, *, task: Task, values: dict[str, Any], lease: TaskLease | None = None
) -> bool:
    """发布阶段条件更新（4.1 幂等转移）：WHERE GENERATING+PUBLISHING；rowcount=0 →
    并发 worker 已转移（发布/失败）→ 不覆盖。不 commit——由调用方提交。"""
    predicates: list[Any] = [
        Task.task_id == task.task_id,
        Task.status == "GENERATING",
        Task.stage == "PUBLISHING",
    ]
    if lease is not None:
        predicates.extend(
            [
                Task.claimed_by == lease.worker_id,
                Task.lease_token == lease.token,
                Task.lease_version == lease.version,
                Task.lease_until.is_not(None),
            ]
        )
    result = cast(
        CursorResult[Any],
        session.execute(update(Task).where(*predicates).values(**values)),
    )
    return result.rowcount == 1


def publish_generated_cards(
    session: Session, *, task: Task, lease: TaskLease | None = None
) -> None:
    """整批原子发布（4.1）：同一短事务内——CAS 门卫（GENERATING+PUBLISHING）→ 校验
    ≥1 张 STAGED 卡 → 全部置 PUBLISHED → 任务 COMPLETED + generated_card_count
    （只统计已发布卡）；0 张有效卡 → 任务 FAILED + TASK_ZERO_CARDS（V25-D-23，
    不显示"完成 0 张"）。调用方 commit；并发 worker 已发布 → rowcount=0 不覆盖不复活。

    发布无 LLM 调用——纯短事务（R-17 不持锁义务天然满足）；失败路径同样条件更新，
    不信任 identity map（并发转移不覆盖）。
    """
    now = format_utc(SystemClock().now_utc())
    if not _publish_guard_update(session, task=task, values={"updated_at": now}, lease=lease):
        return  # 已转移（并发 worker 完成/失败）→ 不覆盖
    staged = (
        session.scalar(
            select(func.count(Card.card_id)).where(
                Card.source_task_id == task.task_id,
                Card.publication_state == "STAGED",
            )
        )
        or 0
    )
    if staged == 0:
        # 0 张有效卡整体失败（4.1 V25-D-23）：任务 FAILED + TASK_ZERO_CARDS，
        # STAGED 卡继续隔离（本分支无卡）
        if _publish_guard_update(
            session,
            task=task,
            values={
                "status": "FAILED",
                "failure_stage": "PUBLISHING",
                "error_code": ErrorCode.TASK_ZERO_CARDS.value,
                "ended_at": now,
                "resumable": 0,
                "updated_at": now,
                "claimed_by": None,
                "lease_token": None,
                "lease_until": None,
                "lease_version": Task.lease_version + 1,
            },
            lease=lease,
        ):
            session.refresh(task)
            finish_operation(
                session,
                task_id=task.task_id,
                status="FAILED",
                now=now,
                reason=ErrorCode.TASK_ZERO_CARDS.value,
            )
            _observe_task_result(task, "FAILED")  # 8.3（R1 M-3）：0 卡整体失败也计数
            logger.warning(
                "task publish failed, zero valid cards",
                extra={"task_id": task.task_id, "error_code": ErrorCode.TASK_ZERO_CARDS.value},
            )
        return
    session.execute(
        update(Card)
        .where(
            Card.source_task_id == task.task_id,
            Card.publication_state == "STAGED",
        )
        .values(publication_state="PUBLISHED")
    )
    if _publish_guard_update(
        session,
        task=task,
        values={
            "status": "COMPLETED",
            "generated_card_count": staged,
            "ended_at": now,
            "resumable": 0,
            "updated_at": now,
            "claimed_by": None,
            "lease_token": None,
            "lease_until": None,
            "lease_version": Task.lease_version + 1,
        },
        lease=lease,
    ):
        session.refresh(task)
        finish_operation(session, task_id=task.task_id, status="COMPLETED", now=now)
        logger.info(
            "task published",
            extra={"task_id": task.task_id, "published_cards": staged},
        )


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


def _fail_task(
    session: Session,
    task: Task,
    *,
    error_code: str,
    failure_stage: str | None = None,
    lease: TaskLease | None = None,
) -> None:
    """任务 FAILED 条件更新（R1 M-2：与 _publish_guard_update 同款 CAS 门卫，不信任
    identity map）——WHERE 当前 DB 状态 == 读取快照（status+stage）：stale worker
    （心跳超时 30 分钟后仍存活）的异常路径不得覆盖并发 worker 已提交的 COMPLETED
    （卡已 PUBLISHED 而任务 FAILED 的不一致），也不得打断并发转移中的任务
    （stage 已前进、另一 worker 在途）。rowcount=0 → expire 放弃旧快照（防调用方
    commit 时身份映射残写覆盖并发结果）。failure_stage 缺省从快照 stage 派生
    （GENERATING/SCORING/PUBLISHING 分支各自归因——spec 6.4/8）；样卡路径
    （stage 为 NULL）不写该列（M-5 裁决：枚举仅正式流水线阶段，NULL 即正确值）。
    观测（8.3）只在实际转移（rowcount=1）时上报。"""
    now = format_utc(SystemClock().now_utc())
    values: dict[str, Any] = {
        "status": "FAILED",
        "error_code": error_code,
        "ended_at": now,
        "resumable": 0,
        "updated_at": now,
        "claimed_by": None,
        "lease_token": None,
        "lease_until": None,
        "lease_version": Task.lease_version + 1,
    }
    derived_stage = failure_stage if failure_stage is not None else task.stage
    if derived_stage is not None:
        values["failure_stage"] = derived_stage
    predicates: list[Any] = [
        Task.task_id == task.task_id,
        Task.status == task.status,
        Task.stage == task.stage,
    ]
    if lease is not None:
        predicates.extend(
            [
                Task.claimed_by == lease.worker_id,
                Task.lease_token == lease.token,
                Task.lease_version == lease.version,
                Task.lease_until.is_not(None),
            ]
        )
    result = cast(
        CursorResult[Any],
        session.execute(update(Task).where(*predicates).values(**values)),
    )
    if result.rowcount == 0:
        session.expire(task)  # 并发已转移 → 不覆盖；放弃快照防残写
        return
    session.refresh(task)
    finish_operation(
        session,
        task_id=task.task_id,
        status="FAILED",
        now=now,
        reason=error_code,
    )
    _observe_task_result(task, "FAILED")  # 8.3：系统级失败也计数（仅实际转移时）


def _complete_sample_task(
    session: Session,
    task: Task,
    settings: Settings,
    client_factory: ClientFactory | None = None,
    lease: TaskLease | None = None,
) -> int:
    """单任务样卡完成（4.1）：读任务配置 → 按启用难度真实生成 1~3 张样卡
    （V5A adapter：密钥解密 → 带 Key client 注入 sample_cards_llm → 章节文本 + 校验；
    章节名取快照，删除后可还原）→ 条件更新持久化样卡 + 配置指纹 → AWAITING。
    返回写入数（0 = 并发 abandon/转移，不复活不写）。不可恢复错误 → FAILED。
    密钥缺失/上游 401（API_KEY_UNAVAILABLE）等 AppError 向 worker 传播 →
    任务 FAILED + error_code（不吞不降格）。"""
    lease = lease or _lease_for(session, task.task_id)
    try:
        config = GenerationConfig(**json.loads(task.generation_config))
        api_key = _decrypt_api_key(session, task=task, settings=settings)
        client = (
            client_factory(api_key)
            if client_factory is not None
            else DeepSeekClient(settings, api_key=api_key)
        )
        try:
            cards = sample_cards_llm(
                session, task=task, config=config, client=client, settings=settings
            )
        finally:
            client.close()
    except (ValueError, TypeError, KeyError):
        # 防御：配置/快照损坏（正常流程不可能）→ 条件更新 FAILED（并发 abandon 不覆盖），
        # 可重试/删除，不悬挂 SAMPLE_GENERATING
        failed_now = format_utc(SystemClock().now_utc())
        failed = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(
                    Task.task_id == task.task_id,
                    Task.status == "SAMPLE_GENERATING",
                    *(
                        [
                            Task.claimed_by == lease.worker_id,
                            Task.lease_token == lease.token,
                            Task.lease_version == lease.version,
                        ]
                        if lease is not None
                        else []
                    ),
                )
                .values(
                    status="FAILED",
                    stage=None,
                    error_code=ErrorCode.GENERATION_FAILED.value,
                    ended_at=failed_now,
                    resumable=0,
                    updated_at=failed_now,
                    claimed_by=None,
                    lease_token=None,
                    lease_until=None,
                    lease_version=Task.lease_version + 1,
                )
            ),
        )
        if failed.rowcount:
            finish_operation(
                session,
                task_id=task.task_id,
                status="FAILED",
                now=failed_now,
                reason=ErrorCode.GENERATION_FAILED.value,
            )
            logger.warning(
                "task sample generation failed",
                extra={"task_id": task.task_id, "internal_reason": "SAMPLE_INPUT_INVALID"},
            )
        return int(failed.rowcount)
    written = complete_samples(
        session,
        task_id=task.task_id,
        cards=cards,
        config_hash=config_fingerprint(config),
        now=format_utc(SystemClock().now_utc()),
        lease=lease,
    )
    if written:
        logger.info(
            "task samples completed",
            extra={"task_id": task.task_id, "sample_count": len(cards)},
        )
    return int(written)


def _sample_worker(
    session: Session, settings: Settings, client_factory: ClientFactory | None = None
) -> int:
    """样卡 worker：真实 LLM 完成全部 SAMPLE_GENERATING 任务（后台请求，幂等键已防
    重复触发；带 Key client 按任务构造，调用后关闭）。

    逐任务异常守卫（与规划/生成/评分 worker 同构）：单任务样卡路径异常（编程/DB
    错误，_complete_sample_task 内层仅捕输入类异常）→ 该任务 FAILED 兜底，不中止
    整轮扫描（其余样卡任务与后续 worker 照常处理）。"""
    worker_id = f"sample-{uuid.uuid4()}"
    now = format_utc(SystemClock().now_utc())
    _recover_expired_task_leases(session, now=now, settings=settings)
    tasks = session.scalars(
        select(Task)
        .where(Task.status == "SAMPLE_GENERATING")
        .order_by(Task.created_at, Task.task_id)
    ).all()
    written = 0
    for task in tasks:
        lease = claim_task(
            session,
            task_id=task.task_id,
            worker_id=worker_id,
            now=format_utc(SystemClock().now_utc()),
            statuses=frozenset({"SAMPLE_GENERATING"}),
        )
        if lease is None:
            continue
        session.refresh(task)
        session.info[_lease_info_key(task.task_id)] = lease
        session.commit()  # 租约先提交，再进入可能耗时/崩溃的外部调用
        try:
            written += _complete_sample_task(session, task, settings, client_factory=client_factory)
        except AppError as exc:
            _fail_task(session, task, error_code=exc.code.value, lease=lease)
            # M-5 裁决：样卡阶段失败不写 failure_stage（_fail_task 条件更新在
            # stage=NULL 时跳过该列，DB 保持 NULL——枚举仅正式流水线阶段，
            # SAMPLE_GENERATING 无对应值，NULL 即正确值）
            logger.warning(
                "task sample generation failed",
                extra={"task_id": task.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(
                session,
                task,
                error_code=ErrorCode.GENERATION_FAILED.value,
                lease=lease,
            )
            logger.warning(
                "task sample generation unexpected failure",
                extra={"task_id": task.task_id},
            )
        finally:
            if release_task(
                session,
                lease,
                now=format_utc(SystemClock().now_utc()),
            ):
                session.commit()
            session.info.pop(_lease_info_key(task.task_id), None)
    return written


def _lease_info_key(task_id: str) -> str:
    return f"task-lease:{task_id}"


def _lease_for(session: Session, task_id: str) -> TaskLease | None:
    value = session.info.get(_lease_info_key(task_id))
    return value if isinstance(value, TaskLease) else None


def _recover_expired_task_leases(session: Session, *, now: str, settings: Settings) -> int:
    """在每轮调度开始时回收 TTL 或心跳过期的执行权。"""
    stale_before = _format_cutoff(now, settings.orphan_timeout_minutes)
    return recover_expired_leases(session, now=now, stale_before=stale_before)


def process_active_tasks(
    session: Session,
    *,
    storage: Any = None,
    settings: Settings | None = None,
    client_factory: ClientFactory | None = None,
    work_quantum: int | None = None,
) -> int:
    """扫描一轮：先样卡 worker（SAMPLE_GENERATING 完成），再规划 worker（CAS 抢占一个
    PLANNING 任务 → run_planning），再生成 worker（GENERATING + stage=GENERATING 任务
    分批生成入库），最后评分 worker（GENERATING + stage=SCORING 孤儿接管）。返回处理任务数。

    storage 预留：V5A 真实 adapter 读取批次内容时使用（当前批次 prompt 仅用知识点 topic）；
    事务：规划 CAS/快照冻结、每组心跳、最终条件更新由 claim/run_planning 内部短事务
    commit（§4.2 冻结原子性、§6.2 STARTED 占位先行）；_execute_task 每批完成后 commit
    （批次事务粒度：批次状态+游标+心跳同事务落库）；任务终态由调用方最终 commit 落库
    （scan_once / handler）。
    settings/client_factory 测试注入；缺省 settings 取环境配置，client_factory 构造生产客户端。
    """
    settings = settings or Settings()
    now = format_utc(SystemClock().now_utc())
    _recover_expired_task_leases(session, now=now, settings=settings)
    # 样卡 worker 入口（V2.5）：完成 SAMPLE_GENERATING → AWAITING_SAMPLE_CONFIRMATION
    sample_count = _sample_worker(session, settings, client_factory=client_factory)
    # 规划 worker 入口（spec §6.1）：每次扫描至多接管一个 PLANNING 任务
    claimed = claim_planning_task(
        session, orphan_timeout_minutes=settings.orphan_timeout_minutes, now=now
    )
    if claimed is not None:
        session.commit()  # CAS1/CAS2 接管 + 章节快照冻结原子提交（§4.2 规划快照冻结时刻）
        planning_worker_id = f"planning-{uuid.uuid4()}"
        planning_lease = claim_task(
            session,
            task_id=claimed.task_id,
            worker_id=planning_worker_id,
            now=format_utc(SystemClock().now_utc()),
            statuses=frozenset({"GENERATING"}),
            stages=frozenset({"PLANNING"}),
        )
        if planning_lease is None:
            # Another process won the execution lease between the planning CAS and this claim.
            # Do not run the external planner with an unfenced snapshot.
            claimed = None
        else:
            session.refresh(claimed)
            session.info[_lease_info_key(claimed.task_id)] = planning_lease
            session.commit()  # 租约先提交，再进入可能耗时的规划调用
        try:
            if claimed is not None and planning_lease is not None:
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
            if claimed is not None and planning_lease is not None:
                _fail_task(
                    session,
                    claimed,
                    error_code=exc.code.value,
                    failure_stage="PLANNING",
                    lease=planning_lease,
                )
                logger.warning(
                    "task planning failed",
                    extra={"task_id": claimed.task_id, "error_code": exc.code.value},
                )
        except Exception:  # noqa: BLE001
            if claimed is not None and planning_lease is not None:
                _fail_task(
                    session,
                    claimed,
                    error_code=ErrorCode.GENERATION_FAILED.value,
                    failure_stage="PLANNING",
                    lease=planning_lease,
                )
                logger.warning(
                    "task planning unexpected failure", extra={"task_id": claimed.task_id}
                )
        finally:
            if planning_lease is not None:
                if release_task(
                    session,
                    planning_lease,
                    now=format_utc(SystemClock().now_utc()),
                ):
                    session.commit()
                if claimed is not None:
                    session.info.pop(_lease_info_key(claimed.task_id), None)
    # 生成 worker 扫描：GENERATING + stage=GENERATING（避免与规划中任务冲突，spec §6.1）
    tasks = session.scalars(
        select(Task)
        .where(Task.status == "GENERATING", Task.stage == "GENERATING")
        .order_by(Task.updated_at, Task.created_at, Task.task_id)
    ).all()
    generation_worker_id = f"generation-{uuid.uuid4()}"
    processed_generation = 0
    processed_generation_ids: set[str] = set()
    for task in tasks:
        if work_quantum is not None and processed_generation >= max(1, work_quantum):
            break
        # Check the heartbeat before claiming: claim_task refreshes updated_at, so doing orphan
        # recovery only after the claim would hide a PROCESSING batch left by a crashed worker.
        _recover_generating_orphans(session, task=task, settings=settings)
        lease = claim_task(
            session,
            task_id=task.task_id,
            worker_id=generation_worker_id,
            now=format_utc(SystemClock().now_utc()),
            statuses=frozenset({"GENERATING"}),
            stages=frozenset({"GENERATING"}),
        )
        if lease is None:
            continue
        session.refresh(task)
        session.info[_lease_info_key(task.task_id)] = lease
        session.commit()  # 先持久化租约，再执行可能包含多次外部调用的批次
        try:
            _execute_task(
                session,
                task,
                settings=settings,
                client_factory=client_factory,
                lease=lease,
                max_batches=work_quantum,
            )
            processed_generation += 1
            processed_generation_ids.add(task.task_id)
        except AppError as exc:
            # 系统级错误（API Key 失效/上游持续不可用）→ FAILED；已入库卡片保留（4.1）
            _fail_task(session, task, error_code=exc.code.value, lease=lease)
            processed_generation += 1
            processed_generation_ids.add(task.task_id)
            logger.warning(
                "task execution failed",
                extra={"task_id": task.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(
                session,
                task,
                error_code=ErrorCode.GENERATION_FAILED.value,
                lease=lease,
            )
            processed_generation += 1
            processed_generation_ids.add(task.task_id)
            logger.warning("task execution unexpected failure", extra={"task_id": task.task_id})
        finally:
            if release_task(
                session,
                lease,
                now=format_utc(SystemClock().now_utc()),
            ):
                session.commit()
            session.info.pop(_lease_info_key(task.task_id), None)
    # 评分 worker 扫描：GENERATING + stage IN (SCORING, PUBLISHING)（spec §8 + 4.1：
    # 心跳超时孤儿可 CAS 接管——SCORING 重跑评分、PUBLISHING 直接发布；心跳新鲜 =
    # 在途 worker 存活，跳过）
    scoring_tasks = session.scalars(
        select(Task)
        .where(Task.status == "GENERATING", Task.stage.in_(["SCORING", "PUBLISHING"]))
        .order_by(Task.created_at)
    ).all()
    acted = 0
    scoring_worker_id = f"scoring-{uuid.uuid4()}"
    for task in scoring_tasks:
        if not _heartbeat_stale(
            task.updated_at,
            format_utc(SystemClock().now_utc()),
            settings.orphan_timeout_minutes,
        ):
            continue
        lease = claim_task(
            session,
            task_id=task.task_id,
            worker_id=scoring_worker_id,
            now=format_utc(SystemClock().now_utc()),
            statuses=frozenset({"GENERATING"}),
            stages=frozenset({"SCORING", "PUBLISHING"}),
        )
        if lease is None:
            continue
        session.refresh(task)
        session.info[_lease_info_key(task.task_id)] = lease
        session.commit()
        try:
            acted += _execute_scoring_task(
                session, task, settings=settings, client_factory=client_factory, lease=lease
            )
        except AppError as exc:
            # 评分 worker 不可恢复错误（资产加载失败等）→ FAILED + failure_stage=SCORING
            _fail_task(session, task, error_code=exc.code.value, lease=lease)
            logger.warning(
                "task scoring failed",
                extra={"task_id": task.task_id, "error_code": exc.code.value},
            )
        except Exception:  # noqa: BLE001
            _fail_task(
                session,
                task,
                error_code=ErrorCode.GENERATION_FAILED.value,
                lease=lease,
            )
            logger.warning("task scoring unexpected failure", extra={"task_id": task.task_id})
        finally:
            if release_task(
                session,
                lease,
                now=format_utc(SystemClock().now_utc()),
            ):
                session.commit()
            session.info.pop(_lease_info_key(task.task_id), None)
    # 处理任务数：同一任务规划 + 生成同轮衔接只计一次；SCORING 孤儿接管按实际行动计数
    claimed_id = claimed.task_id if claimed is not None else None
    generation_count = processed_generation - (
        1 if claimed_id is not None and claimed_id in processed_generation_ids else 0
    )
    return sample_count + generation_count + (1 if claimed else 0) + acted


def _execute_task(
    session: Session,
    task: Task,
    *,
    settings: Settings,
    client_factory: ClientFactory | None,
    lease: TaskLease | None = None,
    max_batches: int | None = None,
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
        # 停留 GENERATING，批次间隙状态转移后不再抢占下一批（停止处理，保留已入库卡）
        completed_in_quantum = 0
        while process_next_batch(session, task_id=task.task_id, client=client) > 0:
            if lease is not None:
                require_lease(
                    session,
                    task_id=task.task_id,
                    worker_id=lease.worker_id,
                    token=lease.token,
                    version=lease.version,
                    now=format_utc(SystemClock().now_utc()),
                )
            task.updated_at = format_utc(SystemClock().now_utc())
            session.commit()
            session.refresh(task)
            if lease is not None:
                heartbeat_now = format_utc(SystemClock().now_utc())
                if not renew_task(session, lease, now=heartbeat_now):
                    session.refresh(task)
                    return
                task.updated_at = heartbeat_now
                session.commit()
                session.refresh(task)
            completed_in_quantum += 1
            if task.status != "GENERATING":
                break
            if max_batches is not None and completed_in_quantum >= max(1, max_batches):
                return
        # 批循环结束 → SCORING 阶段（spec §8 独立阶段）：条件更新 stage='GENERATING' → 'SCORING'
        # （转移 → rowcount=0 → 不进入评分）；评分 worker 异常上抛 → 调用方 FAILED（failure_stage
        # 由 task.stage=SCORING 派生）
        if enter_scoring_stage(session, task_id=task.task_id, settings=settings):
            session.refresh(task)
            run_scoring_stage(session, task=task, settings=settings, client=client)
            session.refresh(task)
            # 原子发布（4.1）：run_scoring_stage 终态条件更新 SCORING → PUBLISHING 成功后，
            # 同一短事务校验 STAGED 卡 → 全部 PUBLISHED → COMPLETED（或 TASK_ZERO_CARDS FAILED）；
            # stage 未到 PUBLISHING（并发转移）→ 不发布，终态由其他 worker 决定
            if task.status == "GENERATING" and task.stage == "PUBLISHING":
                publish_generated_cards(session, task=task, lease=lease)
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
    lease: TaskLease | None = None,
) -> int:
    """SCORING/PUBLISHING 孤儿接管（spec §8 + 4.1 + CAS2 同款心跳判据）：仅心跳超时的
    GENERATING+SCORING/PUBLISHING 任务可被接管（新鲜心跳 = 在途 worker 存活，跳过不干预）。

    - SCORING：CAS 条件更新接管 → mark_stale_unknown（遗留 STARTED → UNKNOWN，仍计上限）
      → 重跑 run_scoring_stage（账本为已尝试游标：已尝试组跳过、未尝试组续跑）→ 原子发布。
    - PUBLISHING（崩溃于 SCORING→PUBLISHING 提交与发布之间）：CAS 条件更新接管 →
      直接执行原子发布（无 LLM 调用）。
    返回实际行动数（1 = 接管，0 = 跳过）。"""
    _require_str(task.updated_at, "任务数据不完整（缺少时间戳）")
    lease = lease or _lease_for(session, task.task_id)
    now = format_utc(SystemClock().now_utc())
    if lease is None and not _heartbeat_stale(
        task.updated_at, now, settings.orphan_timeout_minutes
    ):
        return 0  # 心跳新鲜 → 在途 worker 存活，不干预
    cutoff = _format_cutoff(now, settings.orphan_timeout_minutes)
    if task.stage == "PUBLISHING":
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(
                    Task.task_id == task.task_id,
                    Task.status == "GENERATING",
                    Task.stage == "PUBLISHING",
                    *([Task.updated_at < cutoff] if lease is None else []),
                    *(
                        [
                            Task.claimed_by == lease.worker_id,
                            Task.lease_token == lease.token,
                            Task.lease_version == lease.version,
                            Task.lease_until.is_not(None),
                        ]
                        if lease is not None
                        else []
                    ),
                )
                .values(updated_at=now)
            ),
        )
        if result.rowcount == 0:
            return 0  # 已被其他 worker 接管或状态已转移
        session.refresh(task)
        session.commit()  # 接管心跳提交（发布无 LLM，不持写事务义务）
        publish_generated_cards(session, task=task, lease=lease)
        session.refresh(task)
        if task.status == "COMPLETED":
            _observe_task_result(task, "COMPLETED")  # 8.3：任务结果/耗时上报
        return 1
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task.task_id,
                Task.status == "GENERATING",
                Task.stage == "SCORING",
                *([Task.updated_at < cutoff] if lease is None else []),
                *(
                    [
                        Task.claimed_by == lease.worker_id,
                        Task.lease_token == lease.token,
                        Task.lease_version == lease.version,
                        Task.lease_until.is_not(None),
                    ]
                    if lease is not None
                    else []
                ),
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
        # 原子发布（4.1）：run_scoring_stage 终态条件更新 SCORING → PUBLISHING 成功后
        # 同短事务发布；stage 未到 PUBLISHING（并发转移）→ 不发布
        if task.status == "GENERATING" and task.stage == "PUBLISHING":
            publish_generated_cards(session, task=task, lease=lease)
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
    """扫描一轮：处理全部活跃任务（V3A 同款 session_factory 循环）。返回处理任务数。"""
    with session_factory() as session:
        n = process_active_tasks(
            session,
            storage=storage,
            settings=settings,
            client_factory=client_factory,
            work_quantum=(
                settings.generation_work_quantum_batches if settings is not None else None
            ),
        )
        session.commit()
    return n
