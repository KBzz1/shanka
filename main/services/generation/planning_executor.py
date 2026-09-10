"""planning_executor.py：两阶段规划执行（spec §6.1/§6.2/§6.3/§6.4；V2.5.2 粗+精）。

- `claim_planning_task`：CAS1 首次接管（GENERATING+PLANNING 且未接管 → 心跳/
  started_at 落库）+ 提交前按 §4.2 重读章节最新 name/start/end_page 覆盖
  selected_chapters（规划快照冻结，原子提交）；章节失效 → 同事务 FAILED +
  failure_stage=PLANNING + 内部原因 CHAPTER_SNAPSHOT_STALE（日志区分，
  error_code 用兜底 GENERATION_FAILED）；CAS2 孤儿恢复（心跳超时接管 +
  遗留 STARTED 转 UNKNOWN）。V2.5：用户状态 GENERATING 覆盖规划/生成/评分全程，
  接管只动 internal_stage/心跳，不再写 RUNNING。不 commit——由调用方提交保证
  "接管与快照冻结原子性"。
- `run_planning`（V2.5.2 两阶段）：快照选页 → 粗规划（每章按 planner_coarse_max_input_chars
  连续页分段，每段一次调用产出主题清单，账本恢复复用/预算/心跳）→ 确定性校验
  （tier∈模式允许集过滤/上限截断/标题去重/topic_index 分配）→ 精规划（主题按
  pack_topic_batches 打包成批，批页=主题声明页∪±margin，批难度区间=章区间按批字符
  占比）→ 空产出主题 fine-wide 恢复（整章页重试一次，独立 operation_key）→ 合并去重
  → 条件落库（KnowledgePoint + plan_batches + stage=GENERATING + 难度分布 cursor）。
  空单元三分支（§6.4）：全部操作失败 → FAILED+PLANNING；全部成功但 0 单元 →
  COMPLETED + NO_GENERATION_UNITS；部分成功 → GENERATING + skipped 计数。
- operation_key（database-design）：`planning:coarse:{chapter_id}:{seg}`、
  `planning:fine:{chapter_id}:{batch}`、`planning:fine-wide:{chapter_id}:{topic}`；
  粗/精共用账本 stage='PLANNING'（DB CHECK 域），prompt_name/schema_name 区分。
- 红线 4：normalized_result 只保存通过校验的规范化 topics/units JSON（含服务端
  priority/topic_index/注入 tier），不保存完整 Prompt、原文或原始模型响应。
- 时钟：`now` 显式参数定式（claim 由调用方注入）；run_planning 每次尝试/心跳/终态
  各自读取新时钟（SystemClock，ledger.py 同款 _now 兜底约定）——心跳必须真实推进，
  避免长运行任务被 CAS2 误判孤儿接管。
- 终态一律条件更新（WHERE GENERATING+PLANNING）：并发放弃/转移不覆盖；Key 错误、输入
  漂移与快照非法等即时失败同款 guard（review fix 2/5）。
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import Chapter, KnowledgePoint, LlmCallAttempt, Task, TextChunk
from infra.db.session import format_utc
from infra.llm.deepseek import LlmChatClient, RetryableUpstreamError
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.generation.batches import plan_batches
from services.generation.coarse_validator import normalize_title, validate_and_normalize_topics
from services.generation.ledger import (
    attempt_count,
    create_attempt,
    find_success_result,
    finish_failed,
    finish_success,
    mark_stale_unknown,
)
from services.generation.planner_validator import validate_and_truncate
from services.generation.quota import (
    difficulty_interval,
    expand_page_window,
    interval_for_chapter,
    pack_topic_batches,
)
from services.pdf.text_chunks import load_pages
from services.tasks.lease import TaskLease, renew_task, require_lease
from services.tasks.operations import finish_operation

logger = logging.getLogger(__name__)

_PLANNING_STAGE = "PLANNING"
_GENERATING_STATUS = "GENERATING"  # V2.5 用户七态：规划/生成/评分全程 GENERATING
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # database-design 0：UTC、恒 3 位毫秒


def _now_utc() -> str:
    return format_utc(SystemClock().now_utc())


def _task_lease(session: Session, task_id: str) -> TaskLease | None:
    value = session.info.get(f"task-lease:{task_id}")
    return value if isinstance(value, TaskLease) else None


def _parse_utc(value: str) -> datetime:
    """format_utc 输出 → aware UTC datetime。"""
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def _format_cutoff(now: str, minutes: int) -> str:
    """now - minutes 的 format_utc 字符串（database-design 0 定长格式，字符串比较=时间序）。"""
    return format_utc(_parse_utc(now) - timedelta(minutes=minutes))


def _page_digest(pages: Sequence[TextChunk]) -> list[dict[str, str]]:
    return [{"chunk_id": p.chunk_id, "content_sha256": p.content_sha256} for p in pages]


def coarse_fingerprint(
    pages: Sequence[TextChunk],
    interval: tuple[int, int],
    coverage_mode: str,
    versions: dict[str, str],
) -> str:
    """粗规划段输入指纹（spec §6.2）：页 ID + content_sha256 + 覆盖模式 + 主题区间 + 粗规划资产版本。"""
    payload = {
        "pages": _page_digest(pages),
        "coverage_mode": coverage_mode,
        "topic_interval": {"min": interval[0], "max": interval[1]},
        "planner_coarse_prompt_version": versions["planner_coarse_prompt_version"],
        "planner_coarse_output_schema_version": versions["planner_coarse_output_schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def fine_fingerprint(
    topics: Sequence[dict[str, Any]],
    pages: Sequence[TextChunk],
    interval: dict[str, dict[str, int]],
    coverage_mode: str,
    versions: dict[str, str],
) -> str:
    """精规划批输入指纹：批主题声明 + 批页 + 覆盖模式 + 难度区间 + 精规划资产版本。"""
    payload = {
        "topics": [
            {"topic_index": t["topic_index"], "source_chunk_ids": t["source_chunk_ids"]}
            for t in topics
        ],
        "pages": _page_digest(pages),
        "coverage_mode": coverage_mode,
        "difficulty_interval": {
            d: {
                "min": interval.get(d, {}).get("min", 0),
                "max": interval.get(d, {}).get("max", 0),
            }
            for d in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")
        },
        "planner_prompt_version": versions["planner_prompt_version"],
        "planner_output_schema_version": versions["planner_output_schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- CAS 抢占与快照冻结 ----------


def claim_planning_task(session: Session, *, orphan_timeout_minutes: int, now: str) -> Task | None:
    """规划 worker 抢占（spec §6.1；V2.5 七态）：CAS1 首次接管 + 快照冻结；CAS2 孤儿恢复。

    V2.5：任务经 start 进入 GENERATING（internal_stage=PLANNING）；CAS1 目标为
    `GENERATING + stage=PLANNING + started_at IS NULL`（尚未被任何 worker 接管），
    CAS2 目标为同状态但心跳超时（started_at 已落 = 曾被接管，在途 worker 崩溃）。
    接管不转移用户状态（全程 GENERATING），只落 started_at/心跳与快照冻结。

    不 commit——CAS1 的接管与 selected_chapters 冻结（或章节失效 FAILED）
    由调用方同事务提交，保证"已接管但页码未冻结"的中间状态不落库。
    """
    candidate = session.scalar(
        select(Task)
        .where(
            Task.status == _GENERATING_STATUS,
            Task.stage == _PLANNING_STAGE,
            Task.started_at.is_(None),  # 尚未被接管（CAS1）
            Task.lease_until.is_(None),
        )
        .order_by(Task.created_at, Task.task_id)
        .limit(1)
    )
    if candidate is not None:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(
                    Task.task_id == candidate.task_id,
                    Task.status == _GENERATING_STATUS,
                    Task.stage == _PLANNING_STAGE,
                    Task.started_at.is_(None),
                    Task.lease_until.is_(None),
                )
                .values(
                    started_at=func.coalesce(Task.started_at, now),
                    updated_at=now,
                )
            ),
        )
        if result.rowcount == 1:
            session.refresh(candidate)
            if _freeze_chapter_snapshot(session, task=candidate, now=now):
                return candidate
            return None  # 章节失效 → 已同事务置 FAILED（调用方提交）
    cutoff = _format_cutoff(now, orphan_timeout_minutes)
    orphan = session.scalar(
        select(Task)
        .where(
            Task.status == _GENERATING_STATUS,
            Task.stage == _PLANNING_STAGE,
            Task.started_at.is_not(None),  # 曾被接管（CAS2：仅心跳超时）
            Task.updated_at < cutoff,
            (Task.lease_until.is_(None) | (Task.lease_until <= now)),
        )
        .order_by(Task.updated_at, Task.task_id)
        .limit(1)
    )
    if orphan is not None:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Task)
                .where(
                    Task.task_id == orphan.task_id,
                    Task.status == _GENERATING_STATUS,
                    Task.stage == _PLANNING_STAGE,
                    Task.started_at.is_not(None),
                    Task.updated_at < cutoff,
                    (Task.lease_until.is_(None) | (Task.lease_until <= now)),
                )
                .values(updated_at=now)
            ),
        )
        if result.rowcount == 1:
            session.refresh(orphan)
            mark_stale_unknown(session, task_id=orphan.task_id, stage=_PLANNING_STAGE, now=now)
            return orphan
    return None


def _freeze_chapter_snapshot(session: Session, *, task: Task, now: str) -> bool:
    """CAS1 提交前按 §4.2 重读章节最新 name/start/end_page 覆盖 selected_chapters（冻结）。

    任一章节已删除或不属于该 PDF → 同事务 FAILED + failure_stage=PLANNING +
    兜底错误码 GENERATION_FAILED（内部原因 CHAPTER_SNAPSHOT_STALE 日志区分），返回 False。
    """
    try:
        snapshot = json.loads(task.selected_chapters)
    except (ValueError, TypeError):
        return _stale_fail(session, task=task, now=now)
    if not isinstance(snapshot, list) or any(not isinstance(e, dict) for e in snapshot):
        return _stale_fail(session, task=task, now=now)
    chapter_ids = [e["chapter_id"] for e in snapshot]
    chapters = session.scalars(select(Chapter).where(Chapter.chapter_id.in_(chapter_ids))).all()
    by_id = {ch.chapter_id: ch for ch in chapters}
    for entry in snapshot:
        ch = by_id.get(entry["chapter_id"])
        if ch is None or ch.material_id != entry.get("material_id"):
            return _stale_fail(session, task=task, now=now)
        entry["name"] = ch.name
        entry["start_page"] = ch.start_page
        entry["end_page"] = ch.end_page
    task.selected_chapters = json.dumps(snapshot, ensure_ascii=False)
    return True


def _stale_fail(session: Session, *, task: Task, now: str) -> bool:
    """章节快照失效：任务 FAILED + failure_stage=PLANNING（内部原因 CHAPTER_SNAPSHOT_STALE）。"""
    task.status = "FAILED"
    task.stage = None
    task.failure_stage = _PLANNING_STAGE
    task.error_code = ErrorCode.GENERATION_FAILED.value
    task.ended_at = now
    task.resumable = 0
    finish_operation(
        session,
        task_id=task.task_id,
        status="FAILED",
        now=now,
        reason=ErrorCode.GENERATION_FAILED.value,
    )
    logger.warning(
        "task planning chapter snapshot stale",
        extra={"task_id": task.task_id, "internal_reason": "CHAPTER_SNAPSHOT_STALE"},
    )
    return False


# ---------- 规划执行（两阶段） ----------


def run_planning(
    session: Session, task: Task, *, settings: Settings, client: LlmChatClient
) -> None:
    """执行两阶段规划（spec §6.2；V2.5.2）：粗规划分段盘点 → 主题校验 → 精规划分批展开
    → 空产出主题恢复 → 合并去重 → 三分支条件落库。LLM 调用始终在事务外（§3/§6.2 硬规则）。

    时钟：不捕获 run 级冻结 now——每次尝试/心跳/终态各自读取新时钟（review fix 1：
    心跳必须真实推进，否则长运行任务会被 CAS2 误判孤儿接管）。
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
        # 快照无有效章节：不静默返回（review fix 5）——任务 FAILED，避免 RUNNING+PLANNING 悬挂
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_SNAPSHOT_INVALID",
        )
        return
    # 多资料语义（V25-D-29）：页文本经快照 material_id 读取（不再要求任务持有 file_id）
    if not all(e.get("material_id") for e in chapters):
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_TASK_INCOMPLETE",
        )
        return
    config = json.loads(task.generation_config)
    ratio = config["difficulty_ratio"]
    # V2.5 兼容读取：新配置键 coverage_mode / deep_question（0~100 整数档），
    # 迁移前旧行保留 quantity_tendency / application（0~1 浮点）——统一归一化为比例小数
    mode = config.get("coverage_mode", config.get("quantity_tendency", "BALANCED"))
    ratio_basic = ratio.get("basic", 0)
    ratio_understanding = ratio.get("understanding", 0)
    ratio_deep = ratio.get("deep_question", ratio.get("application", 0))

    def _fraction(value: float) -> float:
        return value / 100 if value > 1 else value

    anchors = {
        "COMPACT": settings.cards_per_10k_compact,
        "BALANCED": settings.cards_per_10k_balanced,
        "EXTENSIVE": settings.cards_per_10k_extensive,
    }

    # 1. 快照选页 + 粗规划分段（planner_coarse_max_input_chars 连续页；单页超限独立成段）
    chapter_pages: list[list[TextChunk]] = []
    chapter_segments: list[list[list[TextChunk]]] = []
    for entry in chapters:
        start = entry.get("start_page")
        end = entry.get("end_page")
        pages = load_pages(
            session,
            material_id=str(entry["material_id"]),
            start_page=int(start) if start is not None else None,
            end_page=int(end) if end is not None else None,
        )
        chapter_pages.append(pages)
        chapter_segments.append(
            _split_groups(pages, max_chars=settings.planner_coarse_max_input_chars)
        )

    def _alive() -> bool:
        session.refresh(task)
        if task.status != _GENERATING_STATUS or task.stage != _PLANNING_STAGE:
            return False  # 已取消/转移 → 停止（不再付费调用）
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

    # 2. 粗规划：每章每段一次调用 → 校验规范化 → 章内合并去重
    skipped_ops = 0
    total_ops = 0
    chapter_topics: list[list[dict[str, Any]]] = []
    for entry, segments in zip(chapters, chapter_segments):
        seg_chars = [sum(p.char_count for p in seg) for seg in segments]
        raw_topic_lists: list[list[dict[str, Any]]] = []
        for si, seg in enumerate(segments):
            if not _alive():
                return
            seg_interval = interval_for_chapter(seg_chars[si], str(mode), anchors)
            fingerprint = coarse_fingerprint(seg, seg_interval, str(mode), versions)
            system_prompt, user_prompt = _build_coarse_prompts(
                chapter=entry,
                pages=seg,
                coverage_mode=str(mode),
                topic_interval=seg_interval,
                settings=settings,
                custom_requirements=config.get("custom_requirements"),
            )
            total_ops += 1
            topics = _run_planning_operation(
                session,
                task,
                settings=settings,
                client=client,
                operation_key=f"planning:coarse:{entry['chapter_id']}:{si}",
                fingerprint=fingerprint,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                validate=_coarse_validator_for(seg, seg_interval, str(mode), settings),
                prompt_name="planner-coarse",
                prompt_version=versions["planner_coarse_prompt_version"],
                schema_name="planner_coarse_output",
                schema_version=versions["planner_coarse_output_schema_version"],
                max_output_tokens=settings.planner_coarse_max_output_tokens,
            )
            if topics is None:
                skipped_ops += 1
                continue
            raw_topic_lists.append(cast(list[dict[str, Any]], topics))
            if not _heartbeat():
                return
        chapter_interval = interval_for_chapter(sum(seg_chars), str(mode), anchors)
        chapter_topics.append(_merge_topics(raw_topic_lists, chapter_interval))

    if task.status != _GENERATING_STATUS or task.stage != _PLANNING_STAGE:
        return

    # 3. 精规划：主题打包成批（先算总批数做硬上限守卫，再逐批调用）
    chapter_batches: list[list[list[dict[str, Any]]]] = []
    chapter_sub_intervals: list[list[dict[str, dict[str, int]]]] = []
    chapter_batch_pages: list[list[list[TextChunk]]] = []
    for entry, pages, topics, segments in zip(
        chapters, chapter_pages, chapter_topics, chapter_segments
    ):
        if not topics:
            chapter_batches.append([])
            chapter_sub_intervals.append([])
            chapter_batch_pages.append([])
            continue
        page_chars = {p.chunk_id: p.char_count for p in pages}
        position_of = {p.chunk_id: i for i, p in enumerate(pages)}
        packed = pack_topic_batches(
            topics,
            page_chars,
            max_chars=settings.planner_max_input_chars,
            max_topics=settings.planner_fine_topics_per_call,
        )
        chapter_interval = interval_for_chapter(sum(page_chars.values()), str(mode), anchors)
        batches: list[list[dict[str, Any]]] = []
        sub_intervals: list[dict[str, dict[str, int]]] = []
        batch_pages_list: list[list[TextChunk]] = []
        for batch_idx in packed:
            batch_topics = [topics[i] for i in batch_idx]
            window = expand_page_window(
                {position_of[cid] for t in batch_topics for cid in t["source_chunk_ids"]},
                len(pages),
                margin=settings.planner_fine_page_margin,
            )
            batch_pages = [pages[pos] for pos in sorted(window)]
            batch_chars = sum(p.char_count for p in batch_pages)
            batches.append(batch_topics)
            batch_pages_list.append(batch_pages)
            sub_intervals.append(
                difficulty_interval(
                    interval_for_chapter(batch_chars, str(mode), anchors),
                    _fraction(ratio_basic),
                    _fraction(ratio_understanding),
                    _fraction(ratio_deep),
                )
            )
        chapter_batches.append(batches)
        chapter_sub_intervals.append(sub_intervals)
        chapter_batch_pages.append(batch_pages_list)

    total_fine_batches = sum(len(b) for b in chapter_batches)
    if total_fine_batches > settings.max_planner_groups_per_task:
        logger.warning(
            "task planning fine batch cap exceeded",
            extra={"task_id": task.task_id, "batches": total_fine_batches},
        )
        _finish_planning_failed(
            session, task, error_code=ErrorCode.GENERATION_FAILED.value, skipped=0
        )
        return

    # 4. 精规划批调用 + 空产出主题 fine-wide 恢复
    merged: list[tuple[dict[str, Any], str]] = []
    produced_topics: dict[str, set[int]] = {}
    succeeded_batches: dict[str, set[int]] = {}
    for entry, pages, topics, batches, sub_intervals, batch_pages_list in zip(
        chapters,
        chapter_pages,
        chapter_topics,
        chapter_batches,
        chapter_sub_intervals,
        chapter_batch_pages,
    ):
        chapter_id = str(entry["chapter_id"])
        for bi, (batch_topics, batch_page_list, interval) in enumerate(
            zip(batches, batch_pages_list, sub_intervals)
        ):
            if not _alive():
                return
            fingerprint = fine_fingerprint(
                batch_topics, batch_page_list, interval, str(mode), versions
            )
            system_prompt, user_prompt = _build_fine_prompts(
                chapter=entry,
                topics=batch_topics,
                pages=batch_page_list,
                coverage_mode=str(mode),
                interval=interval,
                settings=settings,
                custom_requirements=config.get("custom_requirements"),
            )
            total_ops += 1
            units = _run_planning_operation(
                session,
                task,
                settings=settings,
                client=client,
                operation_key=f"planning:fine:{chapter_id}:{bi}",
                fingerprint=fingerprint,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                validate=_fine_validator_for(batch_topics, batch_page_list, interval, settings),
                prompt_name="planner",
                prompt_version=versions["planner_prompt_version"],
                schema_name="planner_output",
                schema_version=versions["planner_output_schema_version"],
                max_output_tokens=settings.planner_max_output_tokens,
            )
            if units is None:
                skipped_ops += 1
                continue
            succeeded_batches.setdefault(chapter_id, set()).add(bi)
            produced = produced_topics.setdefault(chapter_id, set())
            unit_list = cast(list[dict[str, Any]], units)
            for unit in unit_list:
                produced.add(unit["topic_index"])
            merged.extend((unit, chapter_id) for unit in unit_list)
            if not _heartbeat():
                return

    # 4b. fine-wide 恢复：批成功但该主题 0 单元 → 整章页重试一次（独立 key 避开漂移守卫；
    #     每难度上限 1，轻微超出章区间上限有界且罕见——为漏挖主题兜底）
    for entry, pages, topics, batches in zip(
        chapters, chapter_pages, chapter_topics, chapter_batches
    ):
        chapter_id = str(entry["chapter_id"])
        for bi, batch_topics in enumerate(batches):
            if bi not in succeeded_batches.get(chapter_id, set()):
                continue  # 批已失败/跳过——损失计入 skipped，不重复兜底
            for topic in batch_topics:
                if topic["topic_index"] in produced_topics.get(chapter_id, set()):
                    continue
                if not _alive():
                    return
                recovery_interval = {
                    d: {"min": 0, "max": 1} for d in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")
                }
                fingerprint = fine_fingerprint(
                    [topic], pages, recovery_interval, str(mode), versions
                )
                system_prompt, user_prompt = _build_fine_prompts(
                    chapter=entry,
                    topics=[topic],
                    pages=pages,
                    coverage_mode=str(mode),
                    interval=recovery_interval,
                    settings=settings,
                    custom_requirements=config.get("custom_requirements"),
                )
                total_ops += 1
                units = _run_planning_operation(
                    session,
                    task,
                    settings=settings,
                    client=client,
                    operation_key=f"planning:fine-wide:{chapter_id}:{topic['topic_index']}",
                    fingerprint=fingerprint,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    validate=_fine_validator_for([topic], list(pages), recovery_interval, settings),
                    prompt_name="planner",
                    prompt_version=versions["planner_prompt_version"],
                    schema_name="planner_output",
                    schema_version=versions["planner_output_schema_version"],
                    max_output_tokens=settings.planner_max_output_tokens,
                )
                if units is None:
                    skipped_ops += 1
                    continue
                unit_list = cast(list[dict[str, Any]], units)
                produced_topics.setdefault(chapter_id, set()).update(
                    unit["topic_index"] for unit in unit_list
                )
                merged.extend((unit, chapter_id) for unit in unit_list)
                if not _heartbeat():
                    return

    if task.status != _GENERATING_STATUS:
        return  # Key 错误/输入漂移等内部失败已置 FAILED（或外部转移）→ 不再落最终事务
    # 5. 合并：跨批指纹去重 + 全局 priority（§6.2；主题序=批序+组内序由打包顺序保证）
    final_units = _merge_units(merged)
    # 6. 空单元三分支（§6.4）
    if total_ops > 0 and skipped_ops == total_ops:
        _finish_planning_failed(
            session, task, error_code=ErrorCode.GENERATION_FAILED.value, skipped=skipped_ops
        )
        return
    if not final_units:
        _finish_planning_empty(session, task, skipped=skipped_ops)
        return
    _finish_planning_generating(session, task, units=final_units, skipped=skipped_ops)


def _coarse_validator_for(
    seg: Sequence[TextChunk],
    seg_interval: tuple[int, int],
    mode: str,
    settings: Settings,
) -> Callable[[dict[str, Any]], Any]:
    """粗规划段校验闭包（显式绑定段上下文，避免循环变量晚绑定）。"""

    def validate(raw: dict[str, Any]) -> Any:
        return validate_and_normalize_topics(
            raw,
            coverage_mode=mode,
            topic_interval=seg_interval,
            allowed_page_ids={p.chunk_id for p in seg},
            max_chunks_per_topic=settings.max_source_pages_per_unit,
            max_chars_per_topic=settings.generator_max_input_chars,
            page_chars={p.chunk_id: p.char_count for p in seg},
        )

    return validate


def _fine_validator_for(
    batch_topics: list[dict[str, Any]],
    batch_page_list: list[TextChunk],
    interval: dict[str, dict[str, int]],
    settings: Settings,
) -> Callable[[dict[str, Any]], Any]:
    """精规划批校验闭包（显式绑定批上下文）。"""

    def validate(raw: dict[str, Any]) -> Any:
        return validate_and_truncate(
            raw,
            topics=batch_topics,
            allowed_page_ids={p.chunk_id for p in batch_page_list},
            interval=interval,
            max_pages_per_unit=settings.max_source_pages_per_unit,
            max_chars_per_unit=settings.generator_max_input_chars,
            page_chars={p.chunk_id: p.char_count for p in batch_page_list},
        )

    return validate


def _split_groups(pages: list[TextChunk], *, max_chars: int) -> list[list[TextChunk]]:
    """按连续页累计字符拆组（§4.2）：页序贪心累计，超预算开新组；单页超预算独立成组。"""
    groups: list[list[TextChunk]] = []
    current: list[TextChunk] = []
    current_chars = 0
    for page in pages:
        if page.char_count > max_chars:
            if current:
                groups.append(current)
                current = []
                current_chars = 0
            groups.append([page])  # 页级粒度不可再拆（页文本不切分）
            continue
        if current and current_chars + page.char_count > max_chars:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(page)
        current_chars += page.char_count
    if current:
        groups.append(current)
    return groups


def _merge_topics(
    raw_lists: list[list[dict[str, Any]]], chapter_interval: tuple[int, int]
) -> list[dict[str, Any]]:
    """章内主题合并：跨段标题规范化去重（保留首现）→ 截断到章区间上限 → topic_index 1..N。"""
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for topics in raw_lists:
        for topic in topics:
            norm = normalize_title(str(topic["title"]))
            if not norm or norm in seen:
                continue
            seen.add(norm)
            merged.append(topic)
    upper = chapter_interval[1]
    if len(merged) > upper:
        merged = merged[:upper]
    for index, topic in enumerate(merged, start=1):
        topic["topic_index"] = index
    return merged


def _run_planning_operation(
    session: Session,
    task: Task,
    *,
    settings: Settings,
    client: LlmChatClient,
    operation_key: str,
    fingerprint: str,
    system_prompt: str,
    user_prompt: str,
    validate: Callable[[dict[str, Any]], Any],
    prompt_name: str,
    prompt_version: str,
    schema_name: str,
    schema_version: str,
    max_output_tokens: int,
) -> Any | None:
    """单次规划调用（粗/精共用）：输入漂移守卫 → 恢复复用 → 预算 → 尝试循环。

    返回 validate 通过的规范化结果；None = 操作 SKIPPED、停止或任务已 FAILED
    （Key 错误/输入漂移）。validate 抛 AppError 按输出非法走预算内重试（§6.3）。
    """
    # §6.2 输入漂移守卫：该 operation_key 已有账本尝试但 fingerprint 与重推导不一致
    # → 不得错误复用/续跑旧结果，任务以规划输入漂移失败（fail fast，不发调用）
    drifted = session.scalar(
        select(LlmCallAttempt.call_id)
        .where(
            LlmCallAttempt.task_id == task.task_id,
            LlmCallAttempt.stage == _PLANNING_STAGE,
            LlmCallAttempt.operation_key == operation_key,
            LlmCallAttempt.input_fingerprint != fingerprint,
        )
        .limit(1)
    )
    if drifted is not None:
        _fail_planning_inplace(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            internal_reason="PLANNING_INPUT_DRIFT",
        )
        return None
    saved = find_success_result(
        session,
        task_id=task.task_id,
        stage=_PLANNING_STAGE,
        operation_key=operation_key,
        input_fingerprint=fingerprint,
    )
    if saved is not None:
        try:
            result = json.loads(saved)
            assert isinstance(result, list)
            return result  # 恢复复用（§6.2：同 key+fingerprint 的 SUCCESS 不重复调用）
        except (ValueError, TypeError, AssertionError):
            logger.warning(
                "task planning ledger result unreadable, re-planning operation",
                extra={"task_id": task.task_id, "operation_key": operation_key},
            )
    budget = 1 + settings.planning_retry_limit
    if (
        attempt_count(
            session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
        )
        >= budget
    ):
        return None  # 预算耗尽（含 UNKNOWN）→ 操作 SKIPPED（§6.3 预算不重置）
    while True:
        session.refresh(task)
        if task.status != _GENERATING_STATUS or task.stage != _PLANNING_STAGE:
            return None  # 已取消/转移 → 立即停止，不得再付费调用
        attempt_now = _now_utc()  # 每次尝试取新时钟（review fix 1：心跳真实推进）
        attempt_no = (
            attempt_count(
                session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
            )
            + 1
        )
        user_id = task.user_id
        if user_id is None:
            # 防御：user_id 缺失的历史行（V2.3 起旧 device 域行已删除，防御分支保留）
            _fail_planning_inplace(
                session,
                task,
                error_code=ErrorCode.GENERATION_FAILED.value,
                internal_reason="PLANNING_TASK_INCOMPLETE",
            )
            return None
        attempt = create_attempt(
            session,
            user_id=user_id,
            scope_type="TASK",
            scope_id=task.task_id,
            task_id=task.task_id,
            operation_id=task.operation_id,
            stage=_PLANNING_STAGE,
            operation_key=operation_key,
            input_fingerprint=fingerprint,
            attempt_no=attempt_no,
            model=settings.deepseek_model,
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            schema_name=schema_name,
            schema_version=schema_version,
            now=attempt_now,
        )
        task.updated_at = attempt_now  # 心跳与 STARTED 占位同事务（§9 调用前先有已提交 STARTED 行）
        session.commit()
        try:
            result = client.chat(
                user_prompt,
                system_prompt=system_prompt,
                max_tokens=max_output_tokens,
            )
        except RetryableUpstreamError as exc:
            finish_now = _now_utc()
            if exc.code is ErrorCode.API_KEY_UNAVAILABLE and not exc.retryable:
                # Key 错误（401，§6.3）：条件更新 FAILED + PLANNING，不重试
                # （review fix 2：guard 失败/并发取消 → 不覆盖 CANCELLED）
                # The upstream result is definitive even when a competing worker has already
                # moved the task to a terminal state.  Commit that call-ledger fact first; the
                # subsequent guarded task transition may then lose the race without rolling the
                # FAILED attempt back to STARTED.
                finish_failed(session, attempt, error_code=exc.code.value, now=finish_now)
                session.commit()
                if not _planning_guard_update(session, task, values={"updated_at": finish_now}):
                    return None
                session.commit()
                _fail_planning_inplace(session, task, error_code=exc.code.value)
                return None
            # 上游暂时失败（429/5xx/网络）与输出解析失败 → 预算内重试（§6.3）
            if not _planning_guard_update(session, task, values={"updated_at": finish_now}):
                return None
            finish_failed(session, attempt, error_code=exc.code.value, now=finish_now)
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
        except Exception:  # noqa: BLE001 —— 未预期异常按输出类失败走预算重试
            finish_now = _now_utc()
            if not _planning_guard_update(session, task, values={"updated_at": finish_now}):
                return None
            finish_failed(
                session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=finish_now
            )
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
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
        # 事务外校验（§6.3 输出非法 → 预算内重试；红线 4：原始响应不落库）
        try:
            raw = json.loads(result["content"])
            normalized = validate(raw)
        except (ValueError, TypeError, AppError):
            finish_now = _now_utc()
            if not _planning_guard_update(session, task, values={"updated_at": finish_now}):
                return None
            finish_failed(
                session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=finish_now
            )
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
        finish_now = _now_utc()
        if not _planning_guard_update(session, task, values={"updated_at": finish_now}):
            return None
        finish_success(
            session,
            attempt,
            usage=result["usage"],
            http_status=result["http_status"],
            duration_ms=result["duration_ms"],
            normalized_result=json.dumps(normalized, ensure_ascii=False),
            now=finish_now,
        )
        session.commit()
        return normalized


def _attempt_total(session: Session, task: Task, operation_key: str) -> int:
    """本操作尝试数（含全部状态；§9 预算口径）。"""
    return attempt_count(
        session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
    )


def _build_coarse_prompts(
    *,
    chapter: dict[str, Any],
    pages: Sequence[TextChunk],
    coverage_mode: str,
    topic_interval: tuple[int, int],
    settings: Settings,
    custom_requirements: Any,
) -> tuple[str, str]:
    """粗规划双消息组装（spec §5.7）：稳定 system（prompt + schema 原文）+ 动态 user。"""
    system_prompt = (
        f"{load_asset('prompts', 'planner_coarse')}\n\n<PLANNER_COARSE_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'planner_coarse_output')}\n</PLANNER_COARSE_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {
            "chapter_id": chapter["chapter_id"],
            "name": chapter["name"],
            "start_page": chapter["start_page"],
            "end_page": chapter["end_page"],
        },
        "coverage_mode": coverage_mode,
        "topic_interval": {"min": topic_interval[0], "max": topic_interval[1]},
        "limits": {
            "max_source_chunks_per_topic": settings.max_source_pages_per_unit,
            "max_source_chars_per_topic": settings.generator_max_input_chars,
        },
        "source_chunks": [
            {"chunk_id": p.chunk_id, "page_number": p.page_number, "content": p.content}
            for p in pages
        ],
        "custom_requirements": custom_requirements,
    }
    user_prompt = f"<PLANNER_COARSE_INPUT>{safe_json_dumps(payload)}</PLANNER_COARSE_INPUT>"
    return system_prompt, user_prompt


def _build_fine_prompts(
    *,
    chapter: dict[str, Any],
    topics: Sequence[dict[str, Any]],
    pages: Sequence[TextChunk],
    coverage_mode: str,
    interval: dict[str, dict[str, int]],
    settings: Settings,
    custom_requirements: Any,
) -> tuple[str, str]:
    """精规划双消息组装：稳定 system（prompt + schema 原文）+ 动态 user（批主题+批页）。"""
    system_prompt = (
        f"{load_asset('prompts', 'planner')}\n\n<PLANNER_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'planner_output')}\n</PLANNER_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {"name": chapter["name"]},
        "coverage_mode": coverage_mode,
        "topics": [
            {
                "topic_index": t["topic_index"],
                "title": t["title"],
                "coverage_tier": t["coverage_tier"],
                "source_chunk_ids": t["source_chunk_ids"],
            }
            for t in topics
        ],
        "difficulty_interval": interval,
        "limits": {
            "max_source_chunks_per_unit": settings.max_source_pages_per_unit,
            "max_source_chars_per_unit": settings.generator_max_input_chars,
        },
        "source_chunks": [
            {"chunk_id": p.chunk_id, "page_number": p.page_number, "content": p.content}
            for p in pages
        ],
        "custom_requirements": custom_requirements,
    }
    user_prompt = f"<PLANNER_INPUT>{safe_json_dumps(payload)}</PLANNER_INPUT>"
    return system_prompt, user_prompt


def _merge_units(merged: list[tuple[dict[str, Any], str]]) -> list[dict[str, Any]]:
    """跨批去重（§6.2）：指纹 = (learning_objective, target_difficulty, card_type,
    page 序 source_chunk_ids)；按章序/批序/数组顺序保留首次出现，全局 priority 1..N。"""
    seen: set[tuple[Any, ...]] = set()
    result: list[dict[str, Any]] = []
    for unit, chapter_id in merged:
        key = (
            unit["learning_objective"],
            unit["target_difficulty"],
            unit["card_type"],
            tuple(sorted(unit["source_chunk_ids"])),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append({**unit, "chapter_id": chapter_id, "priority": len(result) + 1})
    return result


def _fail_planning_inplace(
    session: Session,
    task: Task,
    *,
    error_code: str,
    internal_reason: str | None = None,
) -> bool:
    """Key 错误/输入漂移/快照非法等即时失败（review fix 2/5）：条件更新
    WHERE RUNNING+PLANNING（rowcount=0 → 并发取消/转移，不落 FAILED，返回 False）；
    内部原因按 CHAPTER_SNAPSHOT_STALE 约定经日志区分。"""
    now = _now_utc()
    if not _planning_guard_update(
        session,
        task,
        values={
            "status": "FAILED",
            "stage": None,
            "failure_stage": _PLANNING_STAGE,
            "error_code": error_code,
            "ended_at": now,
            "resumable": 0,
        },
    ):
        return False
    task.status = "FAILED"
    task.stage = None
    task.failure_stage = _PLANNING_STAGE
    task.error_code = error_code
    task.ended_at = now
    task.resumable = 0
    finish_operation(
        session,
        task_id=task.task_id,
        status="FAILED",
        now=now,
        reason=error_code,
    )
    extra: dict[str, object] = {"task_id": task.task_id, "error_code": error_code}
    if internal_reason is not None:
        extra["internal_reason"] = internal_reason
    logger.warning("task planning failed", extra=extra)
    return True


def _planning_guard_update(session: Session, task: Task, *, values: dict[str, Any]) -> bool:
    """最终短事务条件更新（§6.2 step 7）：WHERE GENERATING+PLANNING；rowcount=0 → 回滚。"""
    session.refresh(task)
    lease = _task_lease(session, task.task_id)
    predicates: list[Any] = [
        Task.task_id == task.task_id,
        Task.status == _GENERATING_STATUS,
        Task.stage == _PLANNING_STAGE,
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
    if values.get("status") in {"FAILED", "COMPLETED"} and lease is not None:
        values = {
            **values,
            "claimed_by": None,
            "lease_token": None,
            "lease_until": None,
            "lease_version": Task.lease_version + 1,
        }
    result = cast(
        CursorResult[Any],
        session.execute(update(Task).where(*predicates).values(**values)),
    )
    if result.rowcount == 0:
        session.rollback()  # 条件不成立（已取消/转移）→ 整事务回滚，不信任 identity map
        return False
    return True


def _finish_planning_failed(session: Session, task: Task, *, error_code: str, skipped: int) -> None:
    """全部规划操作失败（§6.4 分支 2）→ FAILED + failure_stage=PLANNING（条件更新）。"""
    now = _now_utc()
    if not _planning_guard_update(
        session,
        task,
        values={
            "status": "FAILED",
            "stage": None,
            "failure_stage": _PLANNING_STAGE,
            "error_code": error_code,
            "ended_at": now,
            "resumable": 0,
            "skipped_planning_group_count": skipped,
        },
    ):
        return
    task.status = "FAILED"
    task.stage = None
    task.failure_stage = _PLANNING_STAGE
    task.error_code = error_code
    task.ended_at = now
    task.resumable = 0
    task.skipped_planning_group_count = skipped
    finish_operation(
        session,
        task_id=task.task_id,
        status="FAILED",
        now=now,
        reason=error_code,
    )
    logger.warning(
        "task planning failed",
        extra={
            "task_id": task.task_id,
            "error_code": error_code,
            "skipped_planning_group_count": skipped,
        },
    )


def _finish_planning_empty(session: Session, task: Task, *, skipped: int) -> None:
    """0 个合法单元（§6.4 分支 1：全组成功；review fix 4：部分失败+0 成功单元同样
    落到本分支）→ COMPLETED + NO_GENERATION_UNITS（条件更新），skipped 计数保留观测。"""
    now = _now_utc()
    if not _planning_guard_update(
        session,
        task,
        values={
            "status": "COMPLETED",
            "stage": None,
            "completion_reason": "NO_GENERATION_UNITS",
            "total_batch_count": 0,
            "completed_batch_count": 0,
            "skipped_planning_group_count": skipped,
            "ended_at": now,
            "resumable": 0,
            "updated_at": now,
        },
    ):
        return
    task.status = "COMPLETED"
    task.stage = None
    task.completion_reason = "NO_GENERATION_UNITS"
    task.total_batch_count = 0
    task.completed_batch_count = 0
    task.skipped_planning_group_count = skipped
    task.ended_at = now
    task.resumable = 0
    finish_operation(session, task_id=task.task_id, status="COMPLETED", now=now)
    logger.info(
        "task planning empty result",
        extra={
            "task_id": task.task_id,
            "completion_reason": "NO_GENERATION_UNITS",
            "skipped_planning_group_count": skipped,
        },
    )


def _finish_planning_generating(
    session: Session, task: Task, *, units: list[dict[str, Any]], skipped: int
) -> None:
    """最终短事务（§6.2 step 7）：条件更新 → 写 KnowledgePoint + plan_batches +
    stage=GENERATING + skipped 计数 + 难度分布 cursor。rowcount=0 → 回滚返回。"""
    now = _now_utc()
    if not _planning_guard_update(session, task, values={"stage": "GENERATING", "updated_at": now}):
        return
    kps: list[KnowledgePoint] = []
    for unit in units:
        chunk_ids = unit["source_chunk_ids"]
        kps.append(
            KnowledgePoint(
                knowledge_point_id=str(uuid.uuid4()),
                task_id=task.task_id,
                chapter_id=unit["chapter_id"],
                source_chunk_id=chunk_ids[
                    0
                ],  # 兼容投影（spec §3.1；运行时以 source_chunk_ids 为权威）
                topic=unit["learning_objective"],
                priority=unit["priority"],
                status="PENDING",
                target_difficulty=unit["target_difficulty"],
                card_type=unit["card_type"],
                coverage_tier=unit["coverage_tier"],
                source_chunk_ids=json.dumps(chunk_ids, ensure_ascii=False),
            )
        )
    session.add_all(kps)
    session.flush()
    # plan_batches 新签名（1 单元 1 批 + generation_unit_id + 显式 now；spec §7）
    plan_batches(session, task_id=task.task_id, generation_units=kps, now=now)
    task.stage = "GENERATING"
    task.skipped_planning_group_count = skipped
    task.cursor = json.dumps(
        {"difficulty_distribution": _difficulty_distribution(units)}, ensure_ascii=False
    )
    logger.info(
        "task planning completed",
        extra={"task_id": task.task_id, "units": len(kps), "skipped_planning_group_count": skipped},
    )


def _difficulty_distribution(units: list[dict[str, Any]]) -> dict[str, int]:
    """实际难度分布（§3.5 观测；不强制补满配额）。"""
    distribution = {"BASIC": 0, "UNDERSTANDING": 0, "DEEP_QUESTION": 0}
    for unit in units:
        distribution[unit["target_difficulty"]] += 1
    return distribution
