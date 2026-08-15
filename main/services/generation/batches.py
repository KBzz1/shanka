"""services.generation.batches：分批执行核心（spec §7 批=单元 / §9 账本 / §5.7 Prompt 组装）。

- plan_batches：按生成单元建批（spec §7：1 单元 = 1 批，batch_index=单元 priority 序
  1..N，generation_unit_id 显式外键必填）+ 任务游标 total=单元数/completed=0 初始化
  （同事务）；不再有 batch_size 分组与 offset 反推。
- process_next_batch：条件更新抢占下一个可处理批次（PENDING 或 FAILED，原子转
  PROCESSING，rowcount=0 → 下一条/0 → 并发单执行者）→ 批 → 单元（generation_unit_id）
  → Prompt 组装（稳定 system：generator v3 + generator-output schema v2 原文；动态
  user：<GENERATOR_INPUT> 安全 JSON——学习目标/锚定难度/锚定卡型/有序页文本/自定义
  要求）→ 账本记账（operation_key=f"generating:{batch_id}"，输入指纹 = 单元学习目标/
  锚定/有序页 id+content_sha256/资产版本；调用前 STARTED 占位 + 抢占同事务提交 →
  事务外 chat → 终态与卡入库同事务）→ 输出校验（generator-output schema v2 → 卡型=
  锚定 → 数量=1 → Card v1 投影后校验）→ SUCCEEDED / FAILED（重试）/ SKIPPED（预算
  耗尽或合法弃权）。
- 重试预算（generation_retry_limit，共 1+limit 次尝试）以账本尝试数为权威：
  STARTED/SUCCESS/FAILED/UNKNOWN 全部计数；Batch.retry_count/token/版本列只是同一次
  调用结果的兼容投影（§7），不构成第二套预算。
- 安全弃权（§5.3）：合法显式空数组 `{"cards":[]}` → 单元 SKIPPED + SOURCE_INSUFFICIENT
  （不重试）；非 JSON/Schema 非法/卡型不符/多卡才进入重试预算。
- 红线 4：normalized_result 不写入 GENERATING 账本行（仅 PLANNING 保存规范化结果）；
  完整 Prompt、原文与原始响应不落日志/账本。
- 8.3 llm 指标上报统一走 llm_metrics.observe_llm_call（批次生成编排点）。
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

import jsonschema
from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import (
    Batch,
    Card,
    KnowledgePoint,
    LlmCallAttempt,
    ReviewState,
    Task,
    TextChunk,
)
from infra.llm.deepseek import DeepSeekClient, RetryableUpstreamError
from infra.llm.prompts import (
    asset_versions,
    load_asset,
    load_schema_asset,
    safe_json_dumps,
)
from infra.metrics import BATCH_RETRY_TOTAL
from services.generation.ledger import (
    attempt_count,
    create_attempt,
    finish_failed,
    finish_success,
)
from services.generation.llm_metrics import observe_llm_call as _observe_llm_call
from services.generation.response_parse import (
    to_internal_card as _to_internal_card,
)
from services.generation.rubric import batch_quality
from services.generation.schema_validator import load_card_schema, validate_card

logger = logging.getLogger(__name__)

# process_next_batch 直接调用（无 executor session.info 注入）时的兜底默认，与 Settings 默认一致
_DEFAULT_RETRY_LIMIT = 2
_DEFAULT_MAX_INPUT_CHARS = 10_000
_DEFAULT_MAX_OUTPUT_TOKENS = 768

_GENERATING_STAGE = "GENERATING"


def plan_batches(
    session: Session,
    *,
    task_id: str,
    generation_units: Sequence[KnowledgePoint],
    now: str,
) -> None:
    """按生成单元建批（spec §7）：每单元一批、batch_index=单元 priority 序（1..N）、
    generation_unit_id 显式外键 → 任务游标 total=单元数/completed=0 初始化（同事务）。

    Task 9 起签名由 batch_size 分组合并为"1 单元 1 批"；Task 10 定稿参数
    generation_units/now（旧 knowledge_points 参数名随 T9 注释迁移）。
    """
    task = session.get(Task, task_id)
    ordered = sorted(generation_units, key=lambda unit: unit.priority)
    for index, unit in enumerate(ordered, start=1):
        session.add(
            Batch(
                batch_id=str(uuid.uuid4()),
                task_id=task_id,
                batch_index=index,
                status="PENDING",
                generated_item_ids="[]",
                retry_count=0,
                generation_unit_id=unit.knowledge_point_id,
                created_at=now,
            )
        )
    if task is not None:
        task.total_batch_count = len(generation_units)
        task.completed_batch_count = 0


def _claim_next_batch(session: Session, *, task_id: str) -> Batch | None:
    """条件更新抢占：PENDING/FAILED → PROCESSING（原子转移，并发 worker 单执行者）。

    V5B：select 取候选后改条件更新（WHERE status IN (PENDING, FAILED)——不用
    candidate.status（expire_on_commit=False 下 identity map 陈旧快照，另一 worker 置
    FAILED 后恒 rowcount=0 → 死循环））；rowcount=0 → 已被其他 worker 抢占 → 继续取下一条
    （循环内）；全 0/无候选 → None（调用方返回 0）。
    FAILED 必未达重试上限（达上限当次已置 SKIPPED）。
    """
    while True:
        candidate = session.scalar(
            select(Batch)
            .where(
                Batch.task_id == task_id,
                Batch.status.in_(["PENDING", "FAILED"]),
            )
            .order_by(Batch.batch_index)
            .limit(1)
        )
        if candidate is None:
            return None
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Batch)
                .where(
                    Batch.batch_id == candidate.batch_id,
                    Batch.status.in_(["PENDING", "FAILED"]),
                )
                .values(status="PROCESSING")
            ),
        )
        if result.rowcount == 1:
            session.refresh(candidate)
            return candidate
        # 被其他 worker 抢占 → 取下一条（continue 循环）


def process_next_batch(session: Session, *, task_id: str, client: DeepSeekClient) -> int:
    """处理下一个可执行批次（1 批 = 1 生成单元，每次 = 账本一次尝试）。返回处理批次数（0 = 无）。

    账本纪律（spec §9 硬规则）：抢占 + STARTED 占位同事务提交（调用前必须有已提交的
    STARTED 行）→ 事务外 chat → 领域写入（Card/Batch/单元）+ 账本终态同事务（调用方
    commit；提交失败保留 STARTED，恢复时按 UNKNOWN 处理）。
    batch_size 语义已删除（批 = 单元，无 offset 反推）；重试预算（generation_retry_limit）
    以账本尝试数为权威（Batch.retry_count 只是兼容投影）。
    """
    settings = session.info.get("settings")
    retry_limit = (
        settings.generation_retry_limit if isinstance(settings, Settings) else _DEFAULT_RETRY_LIMIT
    )
    max_input_chars = (
        settings.generator_max_input_chars
        if isinstance(settings, Settings)
        else _DEFAULT_MAX_INPUT_CHARS
    )
    max_output_tokens = (
        settings.generator_max_output_tokens
        if isinstance(settings, Settings)
        else _DEFAULT_MAX_OUTPUT_TOKENS
    )
    batch = _claim_next_batch(session, task_id=task_id)  # 条件更新抢占（并发单执行者）
    if batch is None:
        return 0
    task = session.get(Task, task_id)
    if task is None:
        raise AppError(ErrorCode.GENERATION_FAILED, "任务不存在")
    now = task.updated_at
    deck_id = task.deck_id
    if now is None or deck_id is None or task.user_id is None:
        # user_id 非空是账本/卡归属写入前提（P4-3：新写入只写 user_id，DESIGN §5.2）
        raise AppError(ErrorCode.GENERATION_FAILED, "任务数据不完整（缺少时间戳/牌组/用户）")
    unit: KnowledgePoint | None = None
    if batch.generation_unit_id is not None:
        unit = session.get(KnowledgePoint, batch.generation_unit_id)
    if unit is None or unit.task_id != task_id:
        # 旧批次无 generation_unit_id（迁移兼容 NULL）或外键失配：无法按单元锚定生成
        _skip_batch(batch, task=task, now=now, unit=None)
        logger.warning(
            "batch skipped, no generation unit",
            extra={
                "task_id": task_id,
                "batch_id": batch.batch_id,
                "internal_reason": "BATCH_NO_GENERATION_UNIT",
            },
        )
        session.flush()
        return 1
    versions = asset_versions()
    pages = _load_unit_pages(session, unit=unit, max_chars=max_input_chars)
    if not pages:
        # 单元无可用来源页（来源不足的极端情形）：按安全弃权直接 SKIPPED，不发调用
        _skip_batch(batch, task=task, now=now, unit=unit)
        logger.warning(
            "batch skipped, source insufficient",
            extra={
                "task_id": task_id,
                "batch_id": batch.batch_id,
                "internal_reason": "SOURCE_INSUFFICIENT",
            },
        )
        session.flush()
        return 1
    operation_key = f"generating:{batch.batch_id}"
    fingerprint = _input_fingerprint(unit, pages, versions)
    budget = 1 + retry_limit
    if (
        attempt_count(
            session, task_id=task_id, stage=_GENERATING_STAGE, operation_key=operation_key
        )
        >= budget
    ):
        # 账本预算耗尽（含 UNKNOWN 孤儿，§9 预算不重置）→ 批次 SKIPPED，不再发调用；
        # retry_count 投影 = 账本尝试数（崩溃恢复后按尝试续跑，无第二套预算）
        batch.retry_count = attempt_count(
            session, task_id=task_id, stage=_GENERATING_STAGE, operation_key=operation_key
        )
        _skip_batch(batch, task=task, now=now, unit=unit)
        session.flush()
        return 1
    attempt_no = (
        attempt_count(
            session, task_id=task_id, stage=_GENERATING_STAGE, operation_key=operation_key
        )
        + 1
    )
    system_prompt, user_prompt = _build_generator_prompts(task, unit, pages)
    # model：executor 注入 settings 为权威；直接调用（无注入）回退 client 自带 settings
    # （账本记录实际请求的模型，client.chat 使用 client.settings.deepseek_model）
    model = (
        settings.deepseek_model
        if isinstance(settings, Settings)
        else client.settings.deepseek_model
    )
    attempt = create_attempt(
        session,
        user_id=task.user_id,
        scope_type="TASK",
        scope_id=task_id,
        task_id=task_id,
        stage=_GENERATING_STAGE,
        operation_key=operation_key,
        input_fingerprint=fingerprint,
        attempt_no=attempt_no,
        model=model,
        prompt_name="generator",
        prompt_version=versions["generator_prompt_version"],
        schema_name="generator_output",
        schema_version=versions["schema_version"],
        now=now,
    )
    session.commit()  # §9：STARTED 占位 + 批次抢占先提交，之后才发调用（红线 R-17 不持写锁）
    try:
        result = client.chat(
            user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_output_tokens,
        )
    except RetryableUpstreamError as exc:
        if exc.code is ErrorCode.API_KEY_UNAVAILABLE and not exc.retryable:
            # Key 错误（401，§6.3）：记账后上抛 → executor 任务 FAILED，不重试
            finish_failed(session, attempt, error_code=exc.code.value, now=now)
            session.flush()
            raise
        # 上游暂时失败（429/5xx/网络）与输出解析失败 → 预算内重试（§6.3 账本为权威）
        return _finish_attempt_failed(
            session,
            batch=batch,
            task=task,
            unit=unit,
            attempt=attempt,
            error_code=exc.code.value,
            now=now,
            retry_limit=retry_limit,
        )
    except AppError as exc:  # 其他系统级错误：记账后上抛（executor 任务 FAILED）
        finish_failed(session, attempt, error_code=exc.code.value, now=now)
        session.flush()
        raise
    except Exception:
        finish_failed(session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now)
        session.flush()
        raise
    _observe_llm_call(result)  # 8.3：每批一次 chat 的 llm 指标上报
    # 输出校验层（spec §5.3/§5.6）：先原子拒绝原始非法响应，再投影（禁止过滤/降格）
    try:
        raw = json.loads(result["content"])
    except (ValueError, TypeError):
        return _finish_attempt_failed(
            session,
            batch=batch,
            task=task,
            unit=unit,
            attempt=attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            now=now,
            retry_limit=retry_limit,
        )
    if _validate_output_schema(raw):
        return _finish_attempt_failed(
            session,
            batch=batch,
            task=task,
            unit=unit,
            attempt=attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            now=now,
            retry_limit=retry_limit,
        )
    assert isinstance(raw, dict)
    cards = raw["cards"]
    if not cards:
        # 合法显式空数组 = 安全弃权（§5.3）→ SOURCE_INSUFFICIENT 直接 SKIPPED，不重试；
        # 调用本身成功 → 账本 SUCCESS（normalized_result 不写入——红线 4）
        finish_success(
            session,
            attempt,
            usage=result["usage"],
            http_status=result["http_status"],
            duration_ms=result["duration_ms"],
            now=now,
        )
        _project_batch_usage(batch, result, versions)
        batch.retry_count = attempt_no - 1  # 本次尝试成功（弃权）→ 失败次数投影
        _skip_batch(batch, task=task, now=now, unit=unit)
        logger.info(
            "batch skipped, source insufficient",
            extra={
                "task_id": task_id,
                "batch_id": batch.batch_id,
                "internal_reason": "SOURCE_INSUFFICIENT",
            },
        )
        session.flush()
        return 1
    card = cards[0]
    if card["type"] != unit.card_type:
        # 卡型不符锚定（§3.2 双维锚定）→ 输出非法 → 重试预算
        return _finish_attempt_failed(
            session,
            batch=batch,
            task=task,
            unit=unit,
            attempt=attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            now=now,
            retry_limit=retry_limit,
        )
    internal = _to_internal_card(card)  # 确定性 front/back 投影（§5.3）
    if validate_card(internal, load_card_schema()):
        # Card v1 是投影后的第二层校验（§5.1/§5.6）→ 输出非法 → 重试预算
        return _finish_attempt_failed(
            session,
            batch=batch,
            task=task,
            unit=unit,
            attempt=attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            now=now,
            retry_limit=retry_limit,
        )
    inserted, fresh = _insert_card(
        session, task=task, deck_id=deck_id, now=now, batch=batch, unit=unit, internal=internal
    )
    # 领域写入（卡/Batch/单元）+ 账本终态同事务（§9；调用方 commit）
    finish_success(
        session,
        attempt,
        usage=result["usage"],
        http_status=result["http_status"],
        duration_ms=result["duration_ms"],
        now=now,
    )
    _project_batch_usage(batch, result, versions)
    batch.retry_count = attempt_no - 1  # 兼容投影：失败尝试数（本次成功不计）
    batch.status = "SUCCEEDED"
    batch.generated_item_ids = json.dumps([inserted.generation_item_id])
    batch.rubric_version = versions["rubric_version"]
    # V2.5（4.1）：generated_card_count 只在发布时按已发布卡统计（3.4 只统计已发布卡、
    # 失败任务为 0）——生成期不再累加；fresh 仅用于 dedup 观测
    unit.status = "PROCESSED"
    _record_rubric(batch, unit=unit, card=inserted, duplicated=0 if fresh else 1)
    _finish_batch(batch, task, now)
    session.flush()
    return 1


def _finish_attempt_failed(
    session: Session,
    *,
    batch: Batch,
    task: Task,
    unit: KnowledgePoint,
    attempt: LlmCallAttempt,
    error_code: str,
    now: str,
    retry_limit: int,
) -> int:
    """失败尝试落库（账本 FAILED）：预算内 → 批次 FAILED（下次尝试为重试）；预算耗尽
    （尝试数 >= 1+limit，含 UNKNOWN）→ 批次 SKIPPED。Batch.retry_count = 账本尝试数投影。"""
    finish_failed(session, attempt, error_code=error_code, now=now)
    batch.retry_count = attempt.attempt_no  # 投影 = 本次（失败）尝试后的账本尝试数
    if attempt.attempt_no >= 1 + retry_limit:
        _skip_batch(batch, task=task, now=now, unit=unit)
    else:
        batch.status = "FAILED"
        BATCH_RETRY_TOTAL.inc()  # 8.3：批次重试上报
    session.flush()
    return 1


def _skip_batch(batch: Batch, *, task: Task, now: str, unit: KnowledgePoint | None) -> None:
    """批次 SKIPPED 落库（spec §7/§8）：覆盖=0；分布三列置 NULL——无卡不产幽灵单值分布
    （spec §8 口径修正：覆盖=0 与 {单元:1} 分布同列矛盾；单元锚定归因走 generation_unit_id）。"""
    batch.status = "SKIPPED"
    batch.coverage_rate = 0.0
    batch.duplicate_rate = 0.0
    batch.difficulty_deviation = 0.0
    batch.difficulty_distribution = None
    batch.chapter_distribution = None
    batch.card_type_distribution = None
    if unit is not None:
        unit.status = "SKIPPED"
    _finish_batch(batch, task, now)


def _finish_batch(batch: Batch, task: Task, now: str) -> None:
    """游标原子推进（终态批次）：completed_batch_count +1 + ended_at。"""
    task.completed_batch_count = (task.completed_batch_count or 0) + 1
    batch.ended_at = now


def _project_batch_usage(batch: Batch, result: dict[str, Any], versions: dict[str, str]) -> None:
    """Batch 兼容投影（spec §7）：token/版本列由同一次调用结果同步写入（账本为权威，
    不构成第二套预算）。schema_version = 生成调用实际使用的 generator-output schema v2。"""
    usage = result["usage"]
    batch.cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
    batch.cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    batch.output_tokens = usage.get("completion_tokens")
    batch.model = result.get("model")
    batch.http_status = result.get("http_status")
    batch.duration_ms = result.get("duration_ms")
    batch.prompt_version = versions["generator_prompt_version"]
    batch.schema_version = versions["schema_version"]


def _build_generator_prompts(
    task: Task, unit: KnowledgePoint, pages: Sequence[TextChunk]
) -> tuple[str, str]:
    """Generator 双消息组装（spec §5.7 Generator 行）：稳定 system（generator v3 +
    generator-output schema v2 原文）+ 动态 user（<GENERATOR_INPUT> 安全 JSON 信封）。

    动态对象只由服务端构造并 safe_json_dumps（ensure_ascii=False, sort_keys=True,
    separators=(",",":") + 信封边界字符转义）；原文/自定义要求按不可信数据处理；
    关联元数据（generation_unit_id/chunk_id）不进入模型输入。
    """
    system_prompt = (
        f"{load_asset('prompts', 'generator')}\n\n<GENERATOR_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'generator_output')}\n</GENERATOR_OUTPUT_SCHEMA>"
    )
    try:
        config = json.loads(task.generation_config)
    except (ValueError, TypeError):
        config = None
    custom_requirements = config.get("custom_requirements") if isinstance(config, dict) else None
    payload = {
        "learning_objective": unit.topic,
        "target_difficulty": unit.target_difficulty,
        "card_type": unit.card_type,
        "source_material": [{"page_number": p.page_number, "content": p.content} for p in pages],
        "custom_requirements": custom_requirements,
    }
    user_prompt = f"<GENERATOR_INPUT>{safe_json_dumps(payload)}</GENERATOR_INPUT>"
    return system_prompt, user_prompt


def _load_unit_pages(session: Session, *, unit: KnowledgePoint, max_chars: int) -> list[TextChunk]:
    """单元引用页（spec §3.1 权威 source_chunk_ids）按 page_number 升序；总字符量 >
    generator_max_input_chars 时按页序确定性截断（§10 下游上下文防超限；planner 已校验，
    此处为纵深防御，截断后模型只能引用本次实际提供的页）。"""
    try:
        chunk_ids = json.loads(unit.source_chunk_ids or "[]")
    except (ValueError, TypeError):
        chunk_ids = []
    if not isinstance(chunk_ids, list) or not chunk_ids:
        return []
    pages = list(session.scalars(select(TextChunk).where(TextChunk.chunk_id.in_(chunk_ids))).all())
    pages.sort(key=lambda page: (page.page_number, page.chunk_id))
    selected: list[TextChunk] = []
    total = 0
    for page in pages:
        if total + page.char_count > max_chars:
            continue  # 超预算页按页序跳过（确定性截断）
        selected.append(page)
        total += page.char_count
    return selected


def _input_fingerprint(
    unit: KnowledgePoint, pages: Sequence[TextChunk], versions: dict[str, str]
) -> str:
    """生成输入指纹（spec §9）：单元学习目标/锚定 + 有序页 ID 与 content_sha256 + 资产版本。
    完整原文与完整 Prompt 不进入指纹载荷或账本（红线 4）。"""
    payload = {
        "learning_objective": unit.topic,
        "target_difficulty": unit.target_difficulty,
        "card_type": unit.card_type,
        "pages": [{"chunk_id": p.chunk_id, "content_sha256": p.content_sha256} for p in pages],
        "generator_prompt_version": versions["generator_prompt_version"],
        "generator_output_schema_version": versions["schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_output_schema(raw: object) -> list[str]:
    """Generator Output Schema v2 校验（spec §5.3/§5.6：根包装与单卡语义字段的权威）。
    原子拒绝（原始非法响应不得过滤/投影后降格）；空数组合法（安全弃权，卡型/数量在
    通过本 Schema 后由调用方按锚定校验）。"""
    schema = load_schema_asset("generator_output")
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{err.json_path or err.path}: {err.message}" for err in validator.iter_errors(raw)]


def apply_batch_quality(
    batch: Batch,
    *,
    unit: KnowledgePoint,
    cards: Sequence[Card],
    duplicated: int,
    rewrite_duplicate: bool = True,
) -> None:
    """批次质量观测聚合回写（spec §7/§8）：生成期与 SCORING 评分回写期共用。

    批=单元语义：coverage_rate = 该单元是否产出合法卡（0/1）；分布按单元锚定归因
    （target_difficulty/章节/卡型单值）；评分 5 字段不在本函数范围（Card 直写）。
    rewrite_duplicate=False（SCORING 回写期）：不重写 duplicate_rate——dedup 观测是
    生成期一次性记录（dedup-hit 批次 duplicate_rate=1.0），评分重写没有新信息且会把
    该观测清零（review 1/5）。
    """
    quality = batch_quality(
        [
            {
                "type": c.card_type,
                "question": c.question,
                "answer": c.answer,
                "statement": c.statement,
                "explanation": c.explanation,
                "target_difficulty": unit.target_difficulty,
                "chapter_id": unit.chapter_id,
            }
            for c in cards
        ],
        total_kps=1,
        duplicated=duplicated,
    )
    batch.coverage_rate = quality["coverage_rate"]  # 1/1 = 1.0
    if rewrite_duplicate:
        batch.duplicate_rate = quality["duplicate_rate"]
    batch.difficulty_distribution = json.dumps(
        quality["difficulty_distribution"], ensure_ascii=False
    )
    batch.chapter_distribution = json.dumps(quality["chapter_distribution"], ensure_ascii=False)
    batch.card_type_distribution = json.dumps(quality["card_type_distribution"], ensure_ascii=False)
    batch.difficulty_deviation = quality["difficulty_deviation"]


def _record_rubric(batch: Batch, *, unit: KnowledgePoint, card: Card, duplicated: int) -> None:
    """批次 SUCCEEDED 时质量观测（spec §7/§8）：评分 5 字段留 NULL 待 SCORING（T11 回写）；
    质量聚合（coverage/分布）经 apply_batch_quality 与评分回写期共用；
    target_difficulty 由规划锚定在入库时落库（_insert_card）。"""
    apply_batch_quality(batch, unit=unit, cards=[card], duplicated=duplicated)


def _insert_card(
    session: Session,
    *,
    task: Task,
    deck_id: str,
    now: str,
    batch: Batch,
    unit: KnowledgePoint,
    internal: dict[str, Any],
) -> tuple[Card, bool]:
    """单卡入库（V1 模式：Card + ReviewState 初始 NEW；generation_item_id 先查后插防重）。

    seed 保持 `gen|task|batch_index|type|front|back` 稳定（spec：seed 不变）；同 seed
    已入库（恢复/重入边缘）→ 返回既有卡（防重复用，不重复入库），fresh=False。
    target_difficulty 服务端写规划锚定值（§3.2，不要求模型回传）。
    V2.5（4.1）：正式生成写入的卡一律 `STAGED`（可见谓词 3.9 排除），发布时同短事务
    全部置 PUBLISHED；source_task_id/chapter_id 记录生成来源（删除语义 4.1）。
    """
    gen_item = _stable_uuid(
        f"gen|{task.task_id}|{batch.batch_index}|{internal.get('type')}|{internal.get('front')}|{internal.get('back')}"
    )
    existing = session.scalar(select(Card).where(Card.generation_item_id == gen_item))
    if existing is not None:
        return existing, False  # 同 seed 已入库 → 防重复用
    card_id = str(uuid.uuid4())
    card = Card(
        card_id=card_id,
        deck_id=deck_id,
        user_id=task.user_id,
        source="GENERATED",
        position=_next_position(session, deck_id=deck_id),
        front=cast(str, internal["front"]),
        back=cast(str, internal["back"]),
        card_type=cast(str, internal["type"]),
        question=internal.get("question"),
        answer=internal.get("answer"),
        statement=internal.get("statement"),
        answer_boolean=_bool_to_int(internal.get("answer_boolean")),
        explanation=internal.get("explanation"),
        generation_item_id=gen_item,
        source_task_id=task.task_id,  # 生成来源任务（V2.5）
        chapter_id=unit.chapter_id,  # 源章节（V2.5）
        publication_state="STAGED",  # 4.1：正式生成卡先隔离，整批成功才发布
        target_difficulty=unit.target_difficulty,  # 规划锚定落库（§3.2）
        version="v1",
        created_at=now,
        updated_at=now,
    )
    session.add(card)
    session.flush()  # 立即暴露 UNIQUE(deck_id, position) / 部分唯一索引冲突
    session.add(
        ReviewState(
            review_state_id=str(uuid.uuid4()),
            card_id=card_id,
            state="NEW",
            stability=0.0,
            difficulty=1.0,
            due=now,
            reps=0,
            lapses=0,
            updated_at=now,
        )
    )
    session.flush()
    return card, True


def _bool_to_int(value: object) -> int | None:
    """answer_boolean（JSON bool）→ DB Integer（0/1）；非 bool（缺失/非法）→ None。"""
    return int(value) if isinstance(value, bool) else None


def _stable_uuid(seed: str) -> str:
    return str(uuid.UUID(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]))


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1
