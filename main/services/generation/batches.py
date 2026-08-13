"""services.generation.batches：分批执行核心（4.2 批次状态机/重试/游标原子推进）。

- plan_batches：按生成单元建批（spec §7：1 单元 = 1 批，batch_index=单元 priority
  1..N，generation_unit_id 显式外键必填）+ 游标初始化；
- process_next_batch：条件更新抢占下一个可处理批次（PENDING 或 FAILED，FAILED 必未达重试上限，
  WHERE status IN (PENDING, FAILED) 原子转 PROCESSING，rowcount=0 → 下一条/0 → 并发单执行者）→
  adapter.chat（Prompt 组装）→ 响应 JSON 解析 → 逐卡 Schema 校验 → 合法卡入库（V1 模式 +
  generation_item_id 防重）→ SUCCEEDED（≥1 合法卡）/ FAILED（0 合法卡，retry+1）/ 重试达上限
  （retry_count >= limit，共 3 次尝试）→ SKIPPED → 游标 completed_batch_count 与批次状态/计数
  同事务原子推进 → 返回处理批次数（0 = 无）。
- Schema 是唯一入库门槛；Rubric 只观测（Task 3：SUCCEEDED 时 score_card 落
  Card 评分字段 + batch_quality 落 Batch 质量列，不影响入库决策）。
- 8.3 llm 指标上报统一走 llm_metrics.observe_llm_call：本模块是批次生成编排点（每批
  一次 chat，usage 落 Batch 观测列）；单卡重写是另一 chat 编排点
  （services/cards/rewrite.py）——两处共用同一上报（final review Important 1）。
- Key 解密在 executor（仅 infra/llm 路径）：本模块接收已构造带 Key 的 DeepSeekClient。
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import Batch, Card, KnowledgePoint, ReviewState, Task
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import asset_versions, load_asset
from infra.metrics import BATCH_RETRY_TOTAL
from services.generation.llm_metrics import observe_llm_call as _observe_llm_call
from services.generation.response_parse import (
    parse_cards_json as _parse_cards,
)
from services.generation.response_parse import (
    to_internal_card as _to_internal_card,
)
from services.generation.rubric import batch_quality, score_card
from services.generation.schema_validator import load_card_schema, validate_card

logger = logging.getLogger(__name__)

# process_next_batch 直接调用（无 executor session.info 注入）时的兜底默认，与 Settings 默认一致
_DEFAULT_BATCH_SIZE = 3
_DEFAULT_RETRY_LIMIT = 2

_TERMINAL_STATUSES = ("SUCCEEDED", "SKIPPED")

# 难度轮换（V4 语义 carry-forward）：Rubric 观测按 kp priority 批次内轮换三档
_DIFFICULTY_ROTATION = ("BASIC", "UNDERSTANDING", "APPLICATION")


def plan_batches(
    session: Session,
    *,
    task_id: str,
    knowledge_points: Sequence[KnowledgePoint],
) -> None:
    """按生成单元建批（spec §7）：每单元一批、batch_index=单元 priority（1..N）、
    generation_unit_id 显式外键 → 任务游标 total=单元数/completed=0 初始化（同事务）。

    Task 9 起签名由 batch_size 分组合并为"1 单元 1 批"（批粒度 = 生成单元）；旧调用方
    由 T10 生成批改造统一迁移。
    """
    task = session.get(Task, task_id)
    for kp in knowledge_points:
        session.add(
            Batch(
                batch_id=str(uuid.uuid4()),
                task_id=task_id,
                batch_index=kp.priority,
                status="PENDING",
                generated_item_ids="[]",
                retry_count=0,
                generation_unit_id=kp.knowledge_point_id,
                created_at=task.updated_at if task is not None else None,
            )
        )
    if task is not None:
        task.total_batch_count = len(knowledge_points)
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
    """处理下一个可执行批次（每批一次 chat 调用）。返回处理批次数（0 = 无待处理批次）。

    批次状态 + 卡入库 + 游标/计数更新同事务（调用方 commit；失败由调用方回滚）。
    batch_size/retry_limit 从 session.info["settings"] 取（executor 注入），缺省用默认值。
    """
    settings = session.info.get("settings")
    retry_limit = (
        settings.generation_retry_limit if isinstance(settings, Settings) else _DEFAULT_RETRY_LIMIT
    )
    batch_size = settings.batch_size if isinstance(settings, Settings) else _DEFAULT_BATCH_SIZE
    batch = _claim_next_batch(session, task_id=task_id)  # 条件更新抢占（并发单执行者）
    if batch is None:
        return 0
    task = session.get(Task, task_id)
    if task is None:
        raise AppError(ErrorCode.GENERATION_FAILED, "任务不存在")
    now = task.updated_at
    deck_id = task.deck_id
    if now is None or deck_id is None:
        raise AppError(ErrorCode.GENERATION_FAILED, "任务数据不完整（缺少时间戳/牌组）")
    # 批次状态已在 _claim_next_batch 原子置为 PROCESSING
    kps = session.scalars(
        select(KnowledgePoint)
        .where(KnowledgePoint.task_id == task_id)
        .order_by(KnowledgePoint.priority)
        .offset((batch.batch_index - 1) * batch_size)
        .limit(batch_size)
    ).all()
    # Prompt 组装（稳定前缀 + 动态后缀；完整 Prompt 不落日志——红线 4/AC-08）
    prompt_asset = load_asset("prompts", "generator")
    card_schema = json.dumps(load_card_schema(), ensure_ascii=False)
    topic_list = "\n".join(f"- {kp.topic}" for kp in kps)
    # PRD 5.11：稳定块（asset + schema）在前、动态内容（知识点）在后——跨批次前缀稳定，
    # 缓存前缀覆盖 asset + schema（FR-11 观测价值），topic_list 为唯一动态后缀
    prompt = (
        f"{prompt_asset}\n请严格按以下 JSON Schema 输出：\n{card_schema}"
        f"\n本次批次知识点（动态内容）：\n{topic_list}"
    )
    result = client.chat(prompt, api_key="")  # 明文 Key 在 client 构造时注入（executor 解密）
    _observe_llm_call(result)  # 8.3：每批一次 chat 的 llm 指标上报
    inserted, duplicated = _insert_valid_cards(
        session,
        task=task,
        deck_id=deck_id,
        now=now,
        batch=batch,
        cards=_parse_cards(result["content"]),
    )
    # usage/版本观测（structure-contract 3.7；rubric_version 随 rubric 落库）
    usage = result["usage"]
    versions = asset_versions()
    batch.cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
    batch.cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    batch.output_tokens = usage.get("completion_tokens")
    batch.model = result.get("model")
    batch.http_status = result.get("http_status")
    batch.duration_ms = result.get("duration_ms")
    batch.prompt_version = versions["generator_prompt_version"]
    # Batch.schema_version 记录生成调用实际使用的 generator-output schema v2（spec §5.1），
    # 不是 Card 持久化 schema v1（card_schema_version）——后者是服务端投影后的第二层校验
    batch.schema_version = versions["schema_version"]
    # 状态机（4.2）：SUCCEEDED（≥1 合法卡）/ FAILED（0 合法卡，retry+1）/ 重试达上限 → SKIPPED
    task.generated_card_count += len(inserted)
    if inserted:
        batch.status = "SUCCEEDED"
        batch.generated_item_ids = json.dumps([card.generation_item_id for card in inserted])
        batch.rubric_version = versions["rubric_version"]
        _record_rubric(batch, cards=inserted, kps=kps, duplicated=duplicated)
        for kp in kps:
            kp.status = "PROCESSED"
    else:
        # 契约 3.7：最多 2 次重试共 3 次尝试——达上限（retry_count >= limit，即第 3 次失败）才 SKIPPED
        if batch.retry_count >= retry_limit:
            batch.status = "SKIPPED"
            for kp in kps:
                kp.status = "SKIPPED"
        else:
            batch.retry_count += 1  # 还有重试预算 → FAILED（下次尝试为重试）
            batch.status = "FAILED"
            BATCH_RETRY_TOTAL.inc()  # 8.3：批次重试上报
    if batch.status in _TERMINAL_STATUSES:
        task.completed_batch_count = (task.completed_batch_count or 0) + 1  # 游标原子推进
        batch.ended_at = now
    session.flush()
    return 1


def _insert_valid_cards(
    session: Session,
    *,
    task: Task,
    deck_id: str,
    now: str,
    batch: Batch,
    cards: list[dict[str, Any]],
) -> tuple[list[Card], int]:
    """逐卡 Schema 校验 → 合法卡入库（V1 模式 + generation_item_id 防重）。

    返回 (插入的卡列表, 批次内重复跳过数)——重复计数供 Rubric 重复率观测（Task 3）。
    """
    schema = load_card_schema()
    inserted: list[Card] = []
    duplicated = 0
    rejected = 0
    for raw in cards:
        internal = _to_internal_card(raw)
        if validate_card(internal, schema):
            rejected += 1
            continue
        card = _insert_card(
            session, task=task, deck_id=deck_id, now=now, batch=batch, internal=internal
        )
        if card is None:
            duplicated += 1  # 同 seed 已入库（批次内重复内容）→ 重复率观测计数
        else:
            inserted.append(card)
    if rejected:
        logger.info(
            "batch cards rejected by schema",
            extra={"task_id": task.task_id, "batch_index": batch.batch_index, "rejected": rejected},
        )
    return inserted, duplicated


def _record_rubric(
    batch: Batch, *, cards: Sequence[Card], kps: Sequence[KnowledgePoint], duplicated: int
) -> None:
    """批次 SUCCEEDED 时 Rubric 观测落库（Task 3，仅观测不影响入库——红线）。

    逐卡 score_card → Card 5 个评分字段；batch_quality → Batch 质量列（分布 JSON→TEXT）。
    target_difficulty 按批次内 kp priority 轮换（V4 语义）、chapter_id 取 kp.chapter_id
    （carry-forward：批次内卡片按生成顺序轮换映射到本批知识点）。
    """
    quality_cards: list[dict[str, Any]] = []
    for i, card in enumerate(cards):
        kp = kps[i % len(kps)]
        target_difficulty = _DIFFICULTY_ROTATION[(kp.priority - 1) % len(_DIFFICULTY_ROTATION)]
        q: dict[str, Any] = {
            "type": card.card_type,
            "question": card.question,
            "answer": card.answer,
            "statement": card.statement,
            "explanation": card.explanation,
            "target_difficulty": target_difficulty,
            "chapter_id": kp.chapter_id,
        }
        scores = score_card(q)
        card.target_difficulty = target_difficulty
        card.evidence_score = scores["evidence_score"]
        card.correctness_score = scores["correctness_score"]
        card.difficulty_score = scores["difficulty_score"]
        card.learning_value_score = scores["learning_value_score"]
        card.rubric_total_score = scores["rubric_total_score"]
        quality_cards.append(q)
    quality = batch_quality(quality_cards, total_kps=len(kps), duplicated=duplicated)
    batch.coverage_rate = quality["coverage_rate"]
    batch.duplicate_rate = quality["duplicate_rate"]
    batch.difficulty_distribution = json.dumps(
        quality["difficulty_distribution"], ensure_ascii=False
    )
    batch.chapter_distribution = json.dumps(quality["chapter_distribution"], ensure_ascii=False)
    batch.card_type_distribution = json.dumps(quality["card_type_distribution"], ensure_ascii=False)
    batch.difficulty_deviation = quality["difficulty_deviation"]


def _insert_card(
    session: Session,
    *,
    task: Task,
    deck_id: str,
    now: str,
    batch: Batch,
    internal: dict[str, Any],
) -> Card | None:
    """单卡入库（V1 模式：Card + ReviewState 初始 NEW；generation_item_id 先查后插防重）。"""
    gen_item = _stable_uuid(
        f"gen|{task.task_id}|{batch.batch_index}|{internal.get('type')}|{internal.get('front')}|{internal.get('back')}"
    )
    existing = session.scalar(select(Card).where(Card.generation_item_id == gen_item))
    if existing is not None:
        return None  # 同 seed 已入库（批次内重复内容）→ 跳过
    card_id = str(uuid.uuid4())
    card = Card(
        card_id=card_id,
        deck_id=deck_id,
        device_id=task.device_id,
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
        target_difficulty=None,  # Rubric 观测时填（_record_rubric，kp 难度轮换）
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
    return card


def _bool_to_int(value: object) -> int | None:
    """answer_boolean（JSON bool）→ DB Integer（0/1）；非 bool（缺失/非法）→ None。"""
    return int(value) if isinstance(value, bool) else None


def _stable_uuid(seed: str) -> str:
    return str(uuid.UUID(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]))


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1
