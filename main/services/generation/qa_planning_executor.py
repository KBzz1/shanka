"""qa_planning_executor.py：问答直通规划执行（V25-D-43；PRD/structure-contract 3.5 QA_DIRECT）。

与两阶段规划（planning_executor.run_planning）平行的单阶段变体，复用其全部可靠性定式
（CAS 接管/章节快照冻结/账本恢复复用/预算/心跳/条件终态——``_run_planning_operation``）：

- 按 ``planner_coarse_max_input_chars`` 连续块分段，每段一次 qa-planner 调用提取资料
  **已有问答对**（operation_key 前缀 ``planning:qa:{chapter_id}:{seg}``，账本 stage 仍
  'PLANNING'，与 coarse/fine 共存于既有 CHECK 域）；
- 确定性校验（qa_planner_validator：来源接地/上限/双形态 Schema）；
- 跨段合并去重（normalize_question 归一化问题文本 + 卡型）→ 任务级单元硬上限截断；
- 每对映射为一个生成单元：topic=原问题（TRUE_FALSE 为原陈述）、card_type 照录、
  target_difficulty 统一 BASIC（归档参考，PRD V25-D-43 难度弱化）、coverage_tier=None；
- 空产出三分支与 run_planning 同款：全部操作失败 → FAILED+PLANNING；全部成功但 0 对 →
  COMPLETED + NO_GENERATION_UNITS；正常 → ``_finish_planning_generating``（KnowledgePoint +
  plan_batches + stage=GENERATING）——下游 GENERATING/SCORING/PUBLISHING/confirm 零改动。
"""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import ErrorCode
from infra.clock import SystemClock
from infra.db.models import Task, TextChunk
from infra.db.session import format_utc
from infra.llm.deepseek import LlmChatClient
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.generation.planning_executor import (
    _fail_planning_inplace,
    _finish_planning_empty,
    _finish_planning_failed,
    _finish_planning_generating,
    _run_planning_operation,
    _split_groups,
    _task_lease,
)
from services.generation.qa_planner_validator import (
    normalize_question,
    validate_and_normalize_pairs,
)
from services.pdf.text_chunks import load_pages
from services.tasks.lease import renew_task, require_lease

logger = logging.getLogger(__name__)

_PLANNING_STAGE = "PLANNING"
_GENERATING_STATUS = "GENERATING"


def _now_utc() -> str:
    return format_utc(SystemClock().now_utc())


def qa_fingerprint(pages: Sequence[TextChunk], versions: dict[str, str]) -> str:
    """问答提取段输入指纹：页摘要 + qa 资产版本（§6.2 同款漂移守卫口径）。"""
    payload = {
        "pages": [{"chunk_id": p.chunk_id, "content_sha256": p.content_sha256} for p in pages],
        "qa_planner_prompt_version": versions["qa_planner_prompt_version"],
        "qa_planner_output_schema_version": versions["qa_planner_output_schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_qa_planning(
    session: Session, task: Task, *, settings: Settings, client: LlmChatClient
) -> None:
    """执行问答直通规划（V25-D-43）：分段提取资料已有问答对 → 合并去重 → 条件落库。

    LLM 调用始终在事务外（§3/§6.2 硬规则）；每次尝试/心跳/终态各自读取新时钟
    （与 run_planning 同款：心跳必须真实推进，避免 CAS2 误判孤儿接管）。
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
    try:
        snapshot = json.loads(task.selected_chapters)
    except (ValueError, TypeError):
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_SNAPSHOT_INVALID",
        )
        return
    if not isinstance(snapshot, list):
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_SNAPSHOT_INVALID",
        )
        return
    chapters: list[dict[str, Any]] = [dict(e) for e in snapshot if isinstance(e, dict)]
    if not chapters:
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_SNAPSHOT_INVALID",
        )
        return
    if not all(e.get("material_id") for e in chapters):
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_TASK_INCOMPLETE",
        )
        return
    try:
        config = json.loads(task.generation_config)
    except (ValueError, TypeError):
        config = {}
    custom_requirements = config.get("custom_requirements") if isinstance(config, dict) else None

    def _alive() -> bool:
        session.refresh(task)
        if task.status != _GENERATING_STATUS or task.stage != _PLANNING_STAGE:
            return False
        if lease is not None:
            require_lease(
                session,
                task_id=task.task_id,
                worker_id=lease.worker_id,
                token=lease.token,
                version=lease.version,
                now=_now_utc(),
            )
        return True

    def _heartbeat() -> bool:
        if (
            lease is not None
            and task.status == _GENERATING_STATUS
            and task.stage == _PLANNING_STAGE
        ):
            if not renew_task(session, lease, now=_now_utc()):
                session.expire(task)
                return False
            session.commit()
            session.refresh(task)
        return task.status == _GENERATING_STATUS and task.stage == _PLANNING_STAGE

    # 1. 每章选页 + 分段提取（段粒度与粗规划同款：连续块累计字符，单块超限独立成段）
    skipped_ops = 0
    total_ops = 0
    collected: list[tuple[dict[str, Any], str]] = []
    for entry in chapters:
        start = entry.get("start_page")
        end = entry.get("end_page")
        pages = load_pages(
            session,
            material_id=str(entry["material_id"]),
            start_page=int(start) if start is not None else None,
            end_page=int(end) if end is not None else None,
        )
        for si, seg in enumerate(
            _split_groups(pages, max_chars=settings.planner_coarse_max_input_chars)
        ):
            if not _alive():
                return
            fingerprint = qa_fingerprint(seg, versions)
            system_prompt, user_prompt = _build_qa_prompts(
                chapter=entry,
                pages=seg,
                settings=settings,
                custom_requirements=custom_requirements,
            )
            total_ops += 1
            pairs = _run_planning_operation(
                session,
                task,
                settings=settings,
                client=client,
                operation_key=f"planning:qa:{entry['chapter_id']}:{si}",
                fingerprint=fingerprint,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                validate=_qa_validator_for(seg, settings),
                prompt_name="qa-planner",
                prompt_version=versions["qa_planner_prompt_version"],
                schema_name="qa_planner_output",
                schema_version=versions["qa_planner_output_schema_version"],
                max_output_tokens=settings.planner_max_output_tokens,
            )
            if pairs is None:
                skipped_ops += 1
                continue
            collected.extend((pair, str(entry["chapter_id"])) for pair in pairs)
            if not _heartbeat():
                return

    if task.status != _GENERATING_STATUS or task.stage != _PLANNING_STAGE:
        return

    # 2. 跨段合并去重（归一化问题 + 卡型）→ 任务级单元硬上限截断（不重试）
    final_units, truncated = _merge_pairs_to_units(
        collected, cap=settings.max_generation_units_per_task
    )
    if truncated:
        logger.warning(
            "task qa planning unit cap exceeded",
            extra={"task_id": task.task_id, "pairs": len(collected), "kept": len(final_units)},
        )

    # 3. 空单元三分支（与 run_planning 同款 §6.4）
    if total_ops > 0 and skipped_ops == total_ops:
        _finish_planning_failed(
            session, task, error_code=ErrorCode.GENERATION_FAILED.value, skipped=skipped_ops
        )
        return
    if not final_units:
        _finish_planning_empty(session, task, skipped=skipped_ops)
        return
    _finish_planning_generating(session, task, units=final_units, skipped=skipped_ops)


def _qa_validator_for(
    seg: Sequence[TextChunk], settings: Settings
) -> Callable[[dict[str, Any]], Any]:
    """问答提取段校验闭包（显式绑定段上下文，避免循环变量晚绑定）。"""

    def validate(raw: dict[str, Any]) -> Any:
        return validate_and_normalize_pairs(
            raw,
            allowed_page_ids={p.chunk_id for p in seg},
            max_chunks_per_pair=settings.max_source_pages_per_unit,
            max_chars_per_pair=settings.generator_max_input_chars,
            page_chars={p.chunk_id: p.char_count for p in seg},
        )

    return validate


def _merge_pairs_to_units(
    collected: list[tuple[dict[str, Any], str]], *, cap: int
) -> tuple[list[dict[str, Any]], bool]:
    """问答对 → 生成单元：跨段去重（normalize_question + 卡型，保留首现）→ 截断 →
    映射（topic=原问题/原陈述、难度统一 BASIC、tier=None）+ 全局 priority 1..N。

    返回 (units, 是否因任务级上限截断)。
    """
    seen: set[tuple[str, str]] = set()
    units: list[dict[str, Any]] = []
    truncated = False
    for pair, chapter_id in collected:
        question = str(pair["question"] if pair["card_type"] == "QUESTION" else pair["statement"])
        key = (normalize_question(question), str(pair["card_type"]))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        if len(units) >= cap:
            truncated = True
            break
        units.append(
            {
                "chapter_id": chapter_id,
                "learning_objective": question,
                "target_difficulty": "BASIC",  # 归档参考（V25-D-43：难度弱化，不强制配额）
                "card_type": str(pair["card_type"]),
                "coverage_tier": None,
                "source_chunk_ids": list(pair["source_chunk_ids"]),
                "priority": len(units) + 1,
            }
        )
    return units, truncated


def _build_qa_prompts(
    *,
    chapter: dict[str, Any],
    pages: Sequence[TextChunk],
    settings: Settings,
    custom_requirements: Any,
) -> tuple[str, str]:
    """qa-planner 双消息组装（spec §5.7 同款）：稳定 system（prompt + schema 原文）+ 动态 user。"""
    system_prompt = (
        f"{load_asset('prompts', 'qa_planner')}\n\n<QA_PLANNER_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'qa_planner_output')}\n</QA_PLANNER_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {
            "chapter_id": chapter["chapter_id"],
            "name": chapter["name"],
            "start_page": chapter["start_page"],
            "end_page": chapter["end_page"],
        },
        "limits": {
            "max_source_chunks_per_pair": settings.max_source_pages_per_unit,
        },
        "source_chunks": [
            {"chunk_id": p.chunk_id, "page_number": p.page_number, "content": p.content}
            for p in pages
        ],
        "custom_requirements": custom_requirements,
    }
    user_prompt = f"<QA_PLANNER_INPUT>{safe_json_dumps(payload)}</QA_PLANNER_INPUT>"
    return system_prompt, user_prompt
