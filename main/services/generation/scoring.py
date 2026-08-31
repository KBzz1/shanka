"""scoring.py：SCORING 阶段（spec §8/§9/§5.7；Task 11）——分层抽样、合批、回写守卫。

- plan_scoring_groups：候选 = 全单元（经 Batch→Card 有卡）；层 = (chapter_id,
  target_difficulty, card_type)；层内 sha256(task_id+unit_id) 确定性抽样排序；
  BASIC/UNDERSTANDING 按层合批（受 scoring_max_cards_per_call /
  scoring_max_input_chars 双限再拆，卡片+锚定+去重页原文全量计字符）；DEEP_QUESTION
  逐单元；组批后调用数 > max_scoring_calls_per_task → 按层配额（候选数占比，最大
  余数法）+ 哈希序确定性缩减。组数即计划调用数（§8 调用上限口径）。
- run_scoring_stage：逐组——事务内重读 Task（须 GENERATING+SCORING，否则不发请求）→
  create_attempt(STARTED, stage="SCORING", operation_key=f"scoring:{group_key}") +
  心跳同事务 commit → 事务外 chat → 事务外 validate_scores → 事务内：重读各卡
  （version/内容 hash 与指纹重推导不一致 → 整组 finish_failed，内部原因
  STALE_SCORING_INPUT，卡评分留 NULL）→ 回写 Card 5 字段（总分 = 代码计算四维和）+
  Batch 质量 + finish_success + 心跳 commit。任何失败（RetryableUpstreamError/
  AppError/输出非法/STALE）→ finish_failed、不重试、不阻塞，继续下一组。全部组后
  条件更新 WHERE status='GENERATING' AND stage='SCORING' → stage=PUBLISHING
  （rowcount=0 → 不覆盖并发转移）——任务终态（COMPLETED/FAILED）由 executor 的
  原子发布步骤在同一短事务内决定（4.1：整批发布与任务终态原子）。
- 账本纪律（§9）：STARTED 先 commit → 事务外 chat → 终态+领域写同事务；恢复时以
  账本为已尝试游标（同 operation_key 任何尝试状态 ≥1 → 跳过，失败不重试）；
  SCORING 不写 normalized_result（红线 4）；STARTED/FAILED/UNKNOWN 都占
  max_scoring_calls_per_task（scoring_attempt_total 权威）。
- enter_scoring_stage：GENERATING 批循环结束后条件更新 stage='GENERATING' → 'SCORING'。
- 输入指纹（§8）：generation_item_id、Card.version/内容 hash、单元锚定（学习目标/
  难度/卡型/章节）、引用页 content_sha256、scoring prompt/schema/rubric 资产版本。
  完整原文与完整 Prompt 不进入指纹载荷或账本（红线 4）。
- 红线 4：无 API key/完整 Prompt/原始响应入日志或账本。
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import (
    Batch,
    Card,
    KnowledgePoint,
    LlmCallAttempt,
    Task,
    TextChunk,
)
from infra.db.session import format_utc
from infra.llm.deepseek import LlmChatClient, RetryableUpstreamError
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.generation.batches import apply_batch_quality
from services.generation.ledger import (
    attempt_count,
    create_attempt,
    finish_failed,
    finish_success,
    scoring_attempt_total,
)
from services.generation.llm_metrics import observe_llm_call as _observe_llm_call
from services.generation.quota import largest_remainder
from services.generation.scoring_validator import validate_scores
from services.tasks.lease import TaskLease, renew_task, require_lease

logger = logging.getLogger(__name__)

_SCORING_STAGE = "SCORING"
_MERGED_DIFFICULTIES = ("BASIC", "UNDERSTANDING")

# scoring max_tokens 公式（spec §5.7）：min(settings.scoring_max_output_tokens,
# 256 + 128 × items_count)——上限 Settings 化，基数/每项为公式常量
_SCORING_MAX_TOKENS_BASE = 256
_SCORING_MAX_TOKENS_PER_ITEM = 128


@dataclass(frozen=True)
class ScoringGroup:
    """一次评分调用的确定性分组（spec §8）：group_key 由层键 + 单元 ID 摘要构成，
    同任务状态重推导必得相同 group_key/unit_ids/card_ids；operation_key 入账本。"""

    group_key: str
    unit_ids: list[str]
    card_ids: list[str]
    input_fingerprint: str
    operation_key: str


# ---------- 时钟与基础工具 ----------


def _now_utc() -> str:
    return format_utc(SystemClock().now_utc())


def _task_lease(session: Session, task_id: str) -> TaskLease | None:
    value = session.info.get(f"task-lease:{task_id}")
    return value if isinstance(value, TaskLease) else None


def _unit_hash_key(task_id: str, unit_id: str) -> str:
    """层内确定性抽样排序键（spec §8）：sha256(task_id + unit_id)。"""
    return hashlib.sha256(f"{task_id}{unit_id}".encode()).hexdigest()


def _unit_chunk_ids(unit: KnowledgePoint) -> list[str]:
    """单元引用页（spec §3.1 权威 source_chunk_ids）。"""
    try:
        chunk_ids = json.loads(unit.source_chunk_ids or "[]")
    except (ValueError, TypeError):
        return []
    return [c for c in chunk_ids if isinstance(c, str)] if isinstance(chunk_ids, list) else []


def _card_content(card: Card) -> dict[str, Any]:
    """评分输入携带的卡片内容字段（指纹的内容 hash 载荷）。"""
    return {
        "card_type": card.card_type,
        "question": card.question,
        "answer": card.answer,
        "statement": card.statement,
        "explanation": card.explanation,
        "answer_boolean": card.answer_boolean,
    }


def _card_payload(card: Card) -> dict[str, Any]:
    """SCORING_INPUT item 的完整 card（v1 字段 + 布尔值转 bool）。"""
    payload = _card_content(card)
    payload["answer_boolean"] = (
        bool(card.answer_boolean) if card.answer_boolean is not None else None
    )
    return payload


# ---------- 候选与分组规划（spec §8 分层抽样） ----------


def _task_unit_cards(
    session: Session, *, task_id: str, refresh: bool = False
) -> dict[str, list[str]]:
    """任务内单元 → 卡 generation_item_id 列表（Batch→Card；只含实际存在的 Card 行）。

    refresh=True：populate_existing 强制从库重读（回写守卫用——identity map 中的陈旧
    快照不反映并发编辑）。
    """
    batches = session.scalars(
        select(Batch).where(Batch.task_id == task_id, Batch.generation_unit_id.is_not(None))
    ).all()
    mapping: dict[str, list[str]] = {}
    for batch in batches:
        try:
            gen_ids = json.loads(batch.generated_item_ids or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(gen_ids, list) or not gen_ids:
            continue
        assert batch.generation_unit_id is not None
        mapping.setdefault(batch.generation_unit_id, []).extend(
            g for g in gen_ids if isinstance(g, str)
        )
    all_ids = [i for ids in mapping.values() for i in ids]
    existing: set[str] = set()
    if all_ids:
        query = select(Card).where(Card.generation_item_id.in_(all_ids))
        if refresh:
            query = query.execution_options(populate_existing=True)
        existing = {
            c.generation_item_id
            for c in session.scalars(query).all()
            if c.generation_item_id is not None
        }
    return {
        unit_id: sorted(set(ids) & existing)
        for unit_id, ids in mapping.items()
        if set(ids) & existing
    }


def _group_pages(
    entries: list[tuple[KnowledgePoint, list[str]]],
    pages_by_chunk: dict[str, TextChunk],
) -> list[TextChunk]:
    """本组去重页（跨单元按 chunk_id 去重，page_number 升序）。"""
    seen: set[str] = set()
    pages: list[TextChunk] = []
    for unit, _ in entries:
        for chunk_id in _unit_chunk_ids(unit):
            page = pages_by_chunk.get(chunk_id)
            if page is not None and chunk_id not in seen:
                seen.add(chunk_id)
                pages.append(page)
    pages.sort(key=lambda p: (p.page_number, p.chunk_id))
    return pages


def _build_scoring_input(
    entries: list[tuple[KnowledgePoint, list[str]]],
    cards_by_gen: dict[str, Card],
    pages_by_chunk: dict[str, TextChunk],
) -> dict[str, Any]:
    """SCORING_INPUT 载荷（spec §8）：顶层去重 source_chunks + items（每项
    generation_item_id/学习目标/锚定难度/卡型/完整 card/source_chunk_ids）。
    items 保持确定性 group order（层内哈希序）。"""
    pages = _group_pages(entries, pages_by_chunk)
    items: list[dict[str, Any]] = []
    for unit, card_ids in entries:
        chunk_ids = [
            c for c in _unit_chunk_ids(unit) if c in pages_by_chunk
        ]  # 只引用本次实际提供的页（§5.7 引用合法）
        for gen_id in card_ids:
            card = cards_by_gen.get(gen_id)
            if card is None:
                continue
            items.append(
                {
                    "generation_item_id": gen_id,
                    "learning_objective": unit.topic,
                    "target_difficulty": unit.target_difficulty,
                    "card_type": unit.card_type,
                    "card": _card_payload(card),
                    "source_chunk_ids": chunk_ids,
                }
            )
    return {
        "source_chunks": [
            {"chunk_id": p.chunk_id, "page_number": p.page_number, "content": p.content}
            for p in pages
        ],
        "items": items,
    }


def _input_size(
    entries: list[tuple[KnowledgePoint, list[str]]],
    cards_by_gen: dict[str, Card],
    pages_by_chunk: dict[str, TextChunk],
) -> int:
    """全量输入字符数（spec §8）：卡片+锚定+去重页原文的完整 SCORING_INPUT 序列化长度。"""
    return len(safe_json_dumps(_build_scoring_input(entries, cards_by_gen, pages_by_chunk)))


def _group_fingerprint(
    entries: list[tuple[KnowledgePoint, list[str]]],
    cards_by_gen: dict[str, Card],
    pages_by_chunk: dict[str, TextChunk],
    versions: dict[str, str],
) -> str:
    """评分组输入指纹（spec §8）：generation_item_id、Card.version/内容 hash、单元锚定、
    引用页 content_sha256、scoring prompt/schema/rubric 资产版本。"""
    card_fps: list[dict[str, str]] = []
    for _, card_ids in entries:
        for gen_id in card_ids:
            card = cards_by_gen.get(gen_id)
            if card is None:
                continue
            content_hash = hashlib.sha256(
                json.dumps(
                    _card_content(card), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            card_fps.append(
                {
                    "generation_item_id": gen_id,
                    "version": card.version,
                    "content_sha256": content_hash,
                }
            )
    payload = {
        "cards": sorted(card_fps, key=lambda c: c["generation_item_id"]),
        "anchors": sorted(
            [
                {
                    "unit_id": unit.knowledge_point_id,
                    "learning_objective": unit.topic,
                    "target_difficulty": unit.target_difficulty,
                    "card_type": unit.card_type,
                    "chapter_id": unit.chapter_id,
                }
                for unit, _ in entries
            ],
            key=lambda a: a["unit_id"] or "",
        ),
        "pages": sorted(
            [
                {"chunk_id": p.chunk_id, "content_sha256": p.content_sha256}
                for p in _group_pages(entries, pages_by_chunk)
            ],
            key=lambda p: p["chunk_id"],
        ),
        "scoring_prompt_version": versions["scoring_prompt_version"],
        "scoring_output_schema_version": versions["scoring_output_schema_version"],
        "rubric_version": versions["rubric_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_scoring_groups(session: Session, *, task: Task, settings: Settings) -> list[ScoringGroup]:
    """分层抽样 + 合批规划（spec §8）：层 = (chapter_id, target_difficulty, card_type)；
    层内哈希序确定性；BASIC/UNDERSTANDING 按层合批（双限再拆）、DEEP_QUESTION 逐单元；
    组数超 max_scoring_calls_per_task → 按层配额（候选数占比，最大余数法）+ 哈希序缩减。
    """
    lease = _task_lease(session, task.task_id)
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=_now_utc(),
        )
    versions = asset_versions()
    unit_cards = _task_unit_cards(session, task_id=task.task_id)
    if not unit_cards:
        return []
    units_by_id = {
        u.knowledge_point_id: u
        for u in session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.knowledge_point_id.in_(unit_cards))
        ).all()
    }
    # 候选 = 有卡且单元存在的 (层键, 单元, 卡 id 列表)
    candidates: list[tuple[str, KnowledgePoint, list[str]]] = []
    for unit_id, card_ids in unit_cards.items():
        unit = units_by_id.get(unit_id)
        if unit is None:
            continue
        layer = (unit.chapter_id or "", unit.target_difficulty or "", unit.card_type or "")
        candidates.append(("|".join(layer), unit, card_ids))
    # 预载页文本（去重页 + 输入字符计数共用）
    chunk_ids: set[str] = set()
    for _, unit, _ in candidates:
        chunk_ids.update(_unit_chunk_ids(unit))
    pages_by_chunk: dict[str, TextChunk] = {}
    if chunk_ids:
        pages_by_chunk = {
            c.chunk_id: c
            for c in session.scalars(
                select(TextChunk).where(TextChunk.chunk_id.in_(chunk_ids))
            ).all()
        }
    cards_by_gen: dict[str, Card] = {}
    all_card_ids = [i for _, _, ids in candidates for i in ids]
    if all_card_ids:
        cards_by_gen = {
            c.generation_item_id: c
            for c in session.scalars(
                select(Card).where(Card.generation_item_id.in_(all_card_ids))
            ).all()
            if c.generation_item_id is not None
        }
    # 层内哈希序（spec §8：不按 priority，避免质量样本系统性偏高）
    by_layer: dict[str, list[tuple[KnowledgePoint, list[str]]]] = {}
    for layer_key, unit, card_ids in candidates:
        by_layer.setdefault(layer_key, []).append((unit, card_ids))
    for entries in by_layer.values():
        entries.sort(
            key=lambda e: (
                _unit_hash_key(task.task_id, e[0].knowledge_point_id),
                e[0].knowledge_point_id,
            )
        )
    # 合批：BASIC/UNDERSTANDING 按层合批（双限再拆）；DEEP_QUESTION 逐单元
    max_cards = max(1, settings.scoring_max_cards_per_call)
    max_chars = max(1, settings.scoring_max_input_chars)
    layer_groups: dict[str, list[list[tuple[KnowledgePoint, list[str]]]]] = {}
    for layer_key, entries in by_layer.items():
        merged = all((unit.target_difficulty or "") in _MERGED_DIFFICULTIES for unit, _ in entries)
        groups: list[list[tuple[KnowledgePoint, list[str]]]] = []
        current: list[tuple[KnowledgePoint, list[str]]] = []
        for entry in entries:
            if not merged:
                # DEEP_QUESTION 等：逐单元单独调用（每单元 1 卡 = 每卡一次）
                if current:
                    groups.append(current)
                    current = []
                groups.append([entry])
                continue
            tentative = [*current, entry]
            if current and (
                sum(len(ids) for _, ids in tentative) > max_cards
                or _input_size(tentative, cards_by_gen, pages_by_chunk) > max_chars
            ):
                groups.append(current)
                current = [entry]
            else:
                current = tentative
        if current:
            groups.append(current)
        layer_groups[layer_key] = groups
    total_groups = sum(len(g) for g in layer_groups.values())
    layer_labels = sorted(layer_groups)
    kept: dict[str, list[list[tuple[KnowledgePoint, list[str]]]]] = dict(layer_groups)
    if total_groups > max(0, settings.max_scoring_calls_per_task):
        # 层配额 = 层候选单元数占比 × 上限（最大余数法，总和 = 上限）→ 层内保留前 quota 组
        # （组序 = 哈希序，确定性缩减）
        layer_units = {k: len(by_layer[k]) for k in layer_labels}
        total_units = sum(layer_units.values()) or 1
        quotas = largest_remainder(
            [
                settings.max_scoring_calls_per_task * layer_units[k] / total_units
                for k in layer_labels
            ],
            settings.max_scoring_calls_per_task,
            layer_labels,
        )
        kept = {k: layer_groups[k][: quotas[k]] for k in layer_labels}
    result: list[ScoringGroup] = []
    for layer_key in layer_labels:
        for entries in kept[layer_key]:
            card_ids = sorted(i for _, ids in entries for i in ids)
            unit_ids = sorted(u.knowledge_point_id for u, _ in entries)
            fingerprint = _group_fingerprint(entries, cards_by_gen, pages_by_chunk, versions)
            digest = hashlib.sha256(",".join(unit_ids).encode("utf-8")).hexdigest()[:12]
            group_key = f"{layer_key}|{digest}"
            result.append(
                ScoringGroup(
                    group_key=group_key,
                    unit_ids=unit_ids,
                    card_ids=card_ids,
                    input_fingerprint=fingerprint,
                    operation_key=f"scoring:{group_key}",
                )
            )
    return result


# ---------- SCORING 阶段执行 ----------


def _build_scoring_prompts(
    entries: list[tuple[KnowledgePoint, list[str]]],
    cards_by_gen: dict[str, Card],
    pages_by_chunk: dict[str, TextChunk],
) -> tuple[str, str]:
    """Scoring 双消息组装（spec §5.7 Scoring 行）：稳定 system（scoring prompt v2 →
    rubric v2 → scoring-output schema v2 原文）+ 动态 user（<SCORING_INPUT> 安全 JSON）。"""
    system_prompt = (
        f"{load_asset('prompts', 'scoring')}\n\n<SCORING_RUBRIC>\n"
        f"{load_asset('rubrics', 'main')}\n</SCORING_RUBRIC>\n\n<SCORING_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'scoring_output')}\n</SCORING_OUTPUT_SCHEMA>"
    )
    user_prompt = f"<SCORING_INPUT>{safe_json_dumps(_build_scoring_input(entries, cards_by_gen, pages_by_chunk))}</SCORING_INPUT>"
    return system_prompt, user_prompt


def _scoring_max_tokens(item_count: int, *, settings: Settings) -> int:
    """评分输出上限（spec §5.7）：min(settings.scoring_max_output_tokens,
    256 + 128 × items_count)。"""
    return min(
        settings.scoring_max_output_tokens,
        _SCORING_MAX_TOKENS_BASE + _SCORING_MAX_TOKENS_PER_ITEM * item_count,
    )


def _load_group_data(
    session: Session, *, group: ScoringGroup
) -> tuple[list[KnowledgePoint], dict[str, Card], dict[str, TextChunk]]:
    """按组重读单元/卡/页（事务内；回写守卫前可再用 refresh 版重读）。"""
    units = list(
        session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.knowledge_point_id.in_(group.unit_ids))
        ).all()
    )
    cards_by_gen = {
        c.generation_item_id: c
        for c in session.scalars(
            select(Card).where(Card.generation_item_id.in_(group.card_ids))
        ).all()
        if c.generation_item_id is not None
    }
    chunk_ids: set[str] = set()
    for unit in units:
        chunk_ids.update(_unit_chunk_ids(unit))
    pages_by_chunk: dict[str, TextChunk] = {}
    if chunk_ids:
        pages_by_chunk = {
            c.chunk_id: c
            for c in session.scalars(
                select(TextChunk).where(TextChunk.chunk_id.in_(chunk_ids))
            ).all()
        }
    return units, cards_by_gen, pages_by_chunk


def _group_entries(
    units: list[KnowledgePoint], unit_cards: dict[str, list[str]]
) -> list[tuple[KnowledgePoint, list[str]]]:
    return [
        (u, unit_cards[u.knowledge_point_id]) for u in units if u.knowledge_point_id in unit_cards
    ]


def _run_scoring_group(
    session: Session,
    task: Task,
    group: ScoringGroup,
    *,
    settings: Settings,
    client: LlmChatClient,
    versions: dict[str, str],
) -> None:
    """单组评分（spec §8/§9）：组数据+Prompt 组装（与 STARTED 占位同事务提交）→
    STARTED+心跳 commit → 事务外 chat（R-17 不持锁）→ 事务外校验 → 事务内回写守卫
    （重读卡/指纹重推导）→ 5 字段+Batch 质量+finish_success 同事务。

    任何失败 → finish_failed + 心跳 commit，不重试、不阻塞（返回后继续下一组）。
    """
    lease = _task_lease(session, task.task_id)
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=_now_utc(),
        )
    attempt_now = _now_utc()
    attempt_no = (
        attempt_count(
            session, task_id=task.task_id, stage=_SCORING_STAGE, operation_key=group.operation_key
        )
        + 1
    )
    if task.user_id is None:
        # 防御：user_id 缺失的历史行（V2.3 起旧 device 域行已删除，防御分支保留）
        raise AppError(ErrorCode.GENERATION_FAILED, "任务数据不完整（缺少用户）")
    # 组数据 + Prompt 在 STARTED 提交前组装（读取与占位同事务提交——chat 时无打开事务，
    # R-17 不持锁；发送内容与指纹一致由回写守卫兜底）
    units, cards_by_gen, pages_by_chunk = _load_group_data(session, group=group)
    entries = _group_entries(units, _task_unit_cards(session, task_id=task.task_id))
    attempt = create_attempt(
        session,
        user_id=task.user_id,
        scope_type="TASK",
        scope_id=task.task_id,
        task_id=task.task_id,
        operation_id=task.operation_id,
        stage=_SCORING_STAGE,
        operation_key=group.operation_key,
        input_fingerprint=group.input_fingerprint,
        attempt_no=attempt_no,
        model=settings.deepseek_model,
        prompt_name="scoring",
        prompt_version=versions["scoring_prompt_version"],
        schema_name="scoring_output",
        schema_version=versions["scoring_output_schema_version"],
        rubric_version=versions["rubric_version"],
        now=attempt_now,
    )
    task.updated_at = attempt_now  # 心跳与 STARTED 占位同事务（§9）
    if not entries:
        # 组数据已消失（并发删除）：STARTED+FAILED 同事务记账，不调用（恢复跳过）
        if lease is not None:
            require_lease(
                session,
                task_id=task.task_id,
                worker_id=lease.worker_id,
                token=lease.token,
                version=lease.version,
                now=attempt_now,
            )
        finish_failed(
            session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=attempt_now
        )
        session.commit()
        logger.warning(
            "scoring group failed (non-blocking)",
            extra={
                "task_id": task.task_id,
                "operation_key": group.operation_key,
                "error_code": ErrorCode.GENERATION_FAILED.value,
                "internal_reason": "STALE_SCORING_INPUT",
            },
        )
        return
    system_prompt, user_prompt = _build_scoring_prompts(entries, cards_by_gen, pages_by_chunk)
    max_tokens = _scoring_max_tokens(sum(len(ids) for _, ids in entries), settings=settings)
    if lease is not None and not renew_task(session, lease, now=attempt_now):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务执行权已失效，请由新的执行者继续")
    session.commit()  # §9：STARTED 占位 + 心跳先提交，之后才发调用
    try:
        result = client.chat(user_prompt, system_prompt=system_prompt, max_tokens=max_tokens)
    except RetryableUpstreamError as exc:
        # 上游失败（含 Key 错误）→ 不重试不阻塞（§8）
        _finish_group_failed(
            session,
            task,
            attempt,
            error_code=exc.code.value,
            internal_reason="SCORING_UPSTREAM_ERROR",
        )
        return
    except AppError as exc:
        _finish_group_failed(
            session, task, attempt, error_code=exc.code.value, internal_reason="SCORING_CALL_ERROR"
        )
        return
    except Exception:  # noqa: BLE001 —— 未预期异常统一记 GENERATION_FAILED，不阻塞
        _finish_group_failed(
            session,
            task,
            attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="SCORING_CALL_ERROR",
        )
        return
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=_now_utc(),
        )
    _observe_llm_call(result)  # 8.3：成功 chat 即上报
    # 事务外校验（§5.4/§5.6：输出非法 → 整次 FAILED，不落部分分数；红线 4：原始响应不落库）
    try:
        raw = json.loads(result["content"])
        assert isinstance(raw, dict)
        scores = validate_scores(raw, requested_ids=set(group.card_ids))
    except (ValueError, TypeError, AssertionError, AppError):
        _finish_group_failed(
            session,
            task,
            attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="SCORING_OUTPUT_INVALID",
        )
        return
    # 事务内：重读卡（populate_existing——identity map 陈旧快照不反映并发编辑）+ 指纹重推导
    session.expire_all()
    if task.status != "GENERATING" or task.stage != _SCORING_STAGE:
        return  # 调用期间已取消/转移 → 不再回写（STARTED 留给恢复转 UNKNOWN）
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=_now_utc(),
        )
    fresh_unit_cards = _task_unit_cards(session, task_id=task.task_id, refresh=True)
    fresh_units = list(
        session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.knowledge_point_id.in_(group.unit_ids))
        ).all()
    )
    fresh_entries = _group_entries(fresh_units, fresh_unit_cards)
    chunk_ids: set[str] = set()
    for unit, _ in fresh_entries:
        chunk_ids.update(_unit_chunk_ids(unit))
    fresh_pages: dict[str, TextChunk] = {}
    if chunk_ids:
        fresh_pages = {
            c.chunk_id: c
            for c in session.scalars(
                select(TextChunk).where(TextChunk.chunk_id.in_(chunk_ids))
            ).all()
        }
    fresh_cards: dict[str, Card] = {}
    if group.card_ids:
        fresh_cards = {
            c.generation_item_id: c
            for c in session.scalars(
                select(Card)
                .where(Card.generation_item_id.in_(group.card_ids))
                .execution_options(populate_existing=True)
            ).all()
            if c.generation_item_id is not None
        }
    finish_now = _now_utc()
    if (
        not fresh_entries
        or _group_fingerprint(fresh_entries, fresh_cards, fresh_pages, versions)
        != group.input_fingerprint
    ):
        # 版本/内容漂移（用户编辑）→ 整组 FAILED + STALE_SCORING_INPUT，旧分数不写回
        _finish_group_failed(
            session,
            task,
            attempt,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="STALE_SCORING_INPUT",
        )
        return
    # 回写 Card 5 字段（总分 = 代码计算四维和；不触碰 version/updated_at——用户编辑语义）
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=finish_now,
        )
    for gen_id, score in scores.items():
        card = fresh_cards.get(gen_id)
        if card is None:
            continue
        card.evidence_score = score["evidence_score"]
        card.correctness_score = score["correctness_score"]
        card.difficulty_score = score["difficulty_score"]
        card.learning_value_score = score["learning_value_score"]
        card.rubric_total_score = score["rubric_total_score"]
    # Batch 质量字段由评分回写期重写（apply_batch_quality 与生成期共用聚合）；
    # rewrite_duplicate=False：dedup 观测是生成期一次性记录（dedup-hit 批次
    # duplicate_rate=1.0），评分重写无新信息且会清零该观测（review 1/5）
    for unit, card_ids in fresh_entries:
        batch = session.scalar(
            select(Batch).where(
                Batch.task_id == task.task_id,
                Batch.generation_unit_id == unit.knowledge_point_id,
            )
        )
        if batch is None:
            continue
        unit_cards = [fresh_cards[i] for i in card_ids if i in fresh_cards]
        if unit_cards:
            apply_batch_quality(
                batch, unit=unit, cards=unit_cards, duplicated=0, rewrite_duplicate=False
            )
    finish_success(
        session,
        attempt,
        usage=result["usage"],
        http_status=result["http_status"],
        duration_ms=result["duration_ms"],
        now=finish_now,  # normalized_result 不写入（红线 4）
    )
    task.updated_at = finish_now
    session.commit()


def _finish_group_failed(
    session: Session,
    task: Task,
    attempt: LlmCallAttempt,
    *,
    error_code: str,
    internal_reason: str | None = None,
) -> None:
    """组失败落库（§8 非阻塞）：finish_failed + 心跳同事务 commit。"""
    finish_now = _now_utc()
    lease = _task_lease(session, task.task_id)
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=finish_now,
        )
    finish_failed(session, attempt, error_code=error_code, now=finish_now)
    task.updated_at = finish_now
    session.commit()
    logger.warning(
        "scoring group failed (non-blocking)",
        extra={
            "task_id": task.task_id,
            "operation_key": attempt.operation_key,
            "error_code": error_code,
            **({"internal_reason": internal_reason} if internal_reason else {}),
        },
    )


def run_scoring_stage(
    session: Session, *, task: Task, settings: Settings, client: LlmChatClient
) -> None:
    """SCORING 阶段执行（spec §8）：逐组（重读 Task 须 GENERATING+SCORING）→ 全部组后
    条件更新 WHERE status='GENERATING' AND stage='SCORING' → stage=PUBLISHING
    （rowcount=0 → 不覆盖并发转移）。任务终态由 executor 的原子发布决定（4.1：
    GENERATING+PUBLISHING → 校验 ≥1 张 STAGED 卡 → 全部 PUBLISHED → COMPLETED；
    0 张 → FAILED + TASK_ZERO_CARDS）。失败不重试不阻塞；账本为已尝试游标 + 调用上限权威。
    """
    lease = _task_lease(session, task.task_id)
    if lease is not None:
        require_lease(
            session,
            task_id=task.task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=_now_utc(),
        )
    versions = asset_versions()
    groups = plan_scoring_groups(session, task=task, settings=settings)
    for group in groups:
        session.refresh(task)
        if task.status != "GENERATING" or task.stage != _SCORING_STAGE:
            return  # 取消/转移 → 停止（不再付费调用）
        if lease is not None:
            require_lease(
                session,
                task_id=task.task_id,
                worker_id=lease.worker_id,
                token=lease.token,
                version=lease.version,
                now=_now_utc(),
            )
        if scoring_attempt_total(session, task_id=task.task_id) >= max(
            0, settings.max_scoring_calls_per_task
        ):
            # §9 调用前账本条件校验：STARTED/FAILED/UNKNOWN 都占上限。剩余组跳过但
            # 任务仍须走最终条件更新完成（break——不 return，否则 GENERATING+SCORING 悬挂）
            logger.warning(
                "scoring call cap reached, remaining groups skipped",
                extra={
                    "task_id": task.task_id,
                    "attempts": scoring_attempt_total(session, task_id=task.task_id),
                },
            )
            break
        if (
            attempt_count(
                session,
                task_id=task.task_id,
                stage=_SCORING_STAGE,
                operation_key=group.operation_key,
            )
            >= 1
        ):
            continue  # 已尝试游标（恢复；失败不重试）
        _run_scoring_group(
            session, task, group, settings=settings, client=client, versions=versions
        )
        if lease is not None:
            session.refresh(task)
            if task.status != "GENERATING" or task.stage != _SCORING_STAGE:
                return
            heartbeat_now = _now_utc()
            if not renew_task(session, lease, now=heartbeat_now):
                session.expire(task)
                return
            session.commit()
            session.refresh(task)
    now = _now_utc()
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task.task_id,
                Task.status == "GENERATING",
                Task.stage == _SCORING_STAGE,
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
            .values(stage="PUBLISHING", updated_at=now)
        ),
    )
    if result.rowcount == 0:
        return  # 并发转移 → 不覆盖（终态由发布步骤/其他 worker 决定）
    session.refresh(task)
    logger.info(
        "task scoring completed",
        extra={"task_id": task.task_id, "internal_stage": "PUBLISHING"},
    )


def enter_scoring_stage(session: Session, *, task_id: str, settings: Settings) -> bool:
    """GENERATING 批循环结束后进入 SCORING（spec §8 独立阶段）：条件更新
    WHERE status='GENERATING' AND stage='GENERATING' → stage='SCORING'（+ 心跳）。
    rowcount=0 → 并发取消/转移，不覆盖，返回 False。"""
    now = _now_utc()
    lease = _task_lease(session, task_id)
    if lease is not None:
        try:
            require_lease(
                session,
                task_id=task_id,
                worker_id=lease.worker_id,
                token=lease.token,
                version=lease.version,
                now=now,
            )
        except AppError:
            return False
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task_id,
                Task.status == "GENERATING",
                Task.stage == "GENERATING",
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
            .values(stage=_SCORING_STAGE, updated_at=now)
        ),
    )
    return result.rowcount == 1
