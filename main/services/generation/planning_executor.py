"""planning_executor.py：规划执行（spec §6.1/§6.2/§6.3/§6.4；Task 9）。

- `claim_planning_task`：CAS1 首次接管（PENDING+PLANNING → RUNNING）+ 提交前按 §4.2
  重读章节最新 name/start/end_page 覆盖 selected_chapters（规划快照冻结，原子提交）；
  章节失效 → 同事务 FAILED + failure_stage=PLANNING + 内部原因 CHAPTER_SNAPSHOT_STALE
  （日志区分，error_code 用兜底 GENERATION_FAILED）；CAS2 孤儿恢复（心跳超时接管 +
  遗留 STARTED 转 UNKNOWN）。不 commit——由调用方提交保证"接管与快照冻结原子性"。
- `run_planning`：快照选页 → 按 planner_max_input_chars 连续页拆组（组数超上限 →
  FAILED）→ 三层配额（任务→章→组子配额）→ 每组：账本恢复复用 / 预算 / STARTED
  心跳 commit → 事务外 chat → 校验截断 → 终态+心跳 commit → 合并去重 → 条件落库
  （KnowledgePoint + plan_batches + stage=GENERATING + 难度分布 cursor）。空单元
  三分支（§6.4）：全组失败 → FAILED+PLANNING；全组成功但 0 单元 → COMPLETED +
  NO_GENERATION_UNITS；部分成功 → GENERATING + skipped_planning_group_count。
- 红线 4：normalized_result 只保存通过校验的规范化 units JSON（含服务端 priority），
  不保存完整 Prompt、原文或原始模型响应；账本不落 usage 以外的敏感内容。
- 时钟：`now` 显式参数定式（claim 由调用方注入）；run_planning 心跳走
  SystemClock（ledger.py 同款 _now 兜底约定）。
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import Chapter, KnowledgePoint, Task, TextChunk
from infra.db.session import format_utc
from infra.llm.deepseek import DeepSeekClient, RetryableUpstreamError
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.generation.batches import plan_batches
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
    allocate_chapter_quota,
    allocate_group_quota,
    allocate_task_quota,
    task_unit_budget,
)
from services.pdf.text_chunks import load_pages

logger = logging.getLogger(__name__)

# planner max_tokens（spec §5.7/§10 默认 2048；Settings 化由契约同步任务落地前保持常量）
_PLANNER_MAX_OUTPUT_TOKENS = 2048

_PLANNING_STAGE = "PLANNING"
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # database-design 0：UTC、恒 3 位毫秒


def _now_utc() -> str:
    return format_utc(SystemClock().now_utc())


def _parse_utc(value: str) -> datetime:
    """format_utc 输出 → aware UTC datetime。"""
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=UTC)


def _format_cutoff(now: str, minutes: int) -> str:
    """now - minutes 的 format_utc 字符串（database-design 0 定长格式，字符串比较=时间序）。"""
    return format_utc(_parse_utc(now) - timedelta(minutes=minutes))


def group_fingerprint(
    pages: Sequence[TextChunk], quota: dict[str, int], versions: dict[str, str]
) -> str:
    """规划组输入指纹（spec §6.2）：页 ID + content_sha256 + 子配额 + prompt/schema 版本。"""
    payload = {
        "pages": [{"chunk_id": p.chunk_id, "content_sha256": p.content_sha256} for p in pages],
        "difficulty_quota": {d: quota.get(d, 0) for d in ("BASIC", "UNDERSTANDING", "APPLICATION")},
        "planner_prompt_version": versions["planner_prompt_version"],
        "planner_output_schema_version": versions["planner_output_schema_version"],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- CAS 抢占与快照冻结 ----------


def claim_planning_task(session: Session, *, orphan_timeout_minutes: int, now: str) -> Task | None:
    """规划 worker 抢占（spec §6.1）：CAS1 首次接管 + 快照冻结；CAS2 孤儿恢复。

    不 commit——CAS1 的 RUNNING 转移与 selected_chapters 冻结（或章节失效 FAILED）
    由调用方同事务提交，保证"已接管但页码未冻结"的中间状态不落库。
    """
    candidate = session.scalar(
        select(Task)
        .where(Task.status == "PENDING", Task.stage == _PLANNING_STAGE)
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
                    Task.status == "PENDING",
                    Task.stage == _PLANNING_STAGE,
                )
                .values(
                    status="RUNNING",
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
            Task.status == "RUNNING",
            Task.stage == _PLANNING_STAGE,
            Task.updated_at < cutoff,
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
                    Task.status == "RUNNING",
                    Task.stage == _PLANNING_STAGE,
                    Task.updated_at < cutoff,
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
        if ch is None or ch.file_id != task.file_id:
            return _stale_fail(session, task=task, now=now)
        entry["name"] = ch.name
        entry["start_page"] = ch.start_page
        entry["end_page"] = ch.end_page
    task.selected_chapters = json.dumps(snapshot, ensure_ascii=False)
    return True


def _stale_fail(session: Session, *, task: Task, now: str) -> bool:
    """章节快照失效：任务 FAILED + failure_stage=PLANNING（内部原因 CHAPTER_SNAPSHOT_STALE）。"""
    task.status = "FAILED"
    task.failure_stage = _PLANNING_STAGE
    task.error_code = ErrorCode.GENERATION_FAILED.value
    task.ended_at = now
    task.resumable = 0
    logger.warning(
        "task planning chapter snapshot stale",
        extra={"task_id": task.task_id, "internal_reason": "CHAPTER_SNAPSHOT_STALE"},
    )
    return False


# ---------- 规划执行 ----------


def run_planning(
    session: Session, task: Task, *, settings: Settings, client: DeepSeekClient
) -> None:
    """执行规划（spec §6.2）：选页拆组 → 组调用（账本恢复/预算/STARTED→chat→终态）→
    合并去重 → 三分支条件落库。LLM 调用始终在事务外（§3/§6.2 硬规则）。"""
    now = _now_utc()
    versions = asset_versions()
    snapshot = json.loads(task.selected_chapters)
    assert isinstance(snapshot, list)
    chapters: list[dict[str, Any]] = [dict(e) for e in snapshot if isinstance(e, dict)]
    if not chapters:
        return
    file_id = task.file_id
    if file_id is None:
        return  # 任务数据不完整（创建时必填；防御性短路）
    # 1. 快照选页 + 连续页拆组（§4.2）；组数超上限 → FAILED（§6.3 硬上限）
    chapter_groups: list[list[list[TextChunk]]] = []
    for entry in chapters:
        pages = load_pages(
            session,
            file_id=file_id,
            start_page=int(entry["start_page"]),
            end_page=int(entry["end_page"]),
        )
        chapter_groups.append(_split_groups(pages, max_chars=settings.planner_max_input_chars))
    total_groups = sum(len(g) for g in chapter_groups)
    if total_groups > settings.max_planner_groups_per_task:
        logger.warning(
            "task planning group cap exceeded",
            extra={"task_id": task.task_id, "groups": total_groups},
        )
        _finish_planning_failed(
            session, task, error_code=ErrorCode.GENERATION_FAILED.value, now=now, skipped=0
        )
        return
    # 2. 三层配额（§3.5）：任务总配额 → 章配额 → 组子配额（char_count 占比）
    config = json.loads(task.generation_config)
    ratio = config["difficulty_ratio"]
    task_quota = allocate_task_quota(
        task_unit_budget(len(chapters), config["quantity_tendency"]),
        ratio["basic"],
        ratio["understanding"],
        ratio["application"],
    )
    chapter_quotas = allocate_chapter_quota(task_quota, len(chapters))
    # 3. 组调用（账本恢复/预算；STARTED 心跳 commit → 事务外 chat → 终态+心跳 commit）
    skipped_groups = 0
    merged: list[tuple[dict[str, Any], str]] = []
    for ci, (entry, groups) in enumerate(zip(chapters, chapter_groups)):
        char_counts = [sum(p.char_count for p in group) for group in groups]
        sub_quotas = allocate_group_quota(chapter_quotas[ci], char_counts)
        for gi, group in enumerate(groups):
            session.refresh(task)
            if task.status != "RUNNING" or task.stage != _PLANNING_STAGE:
                return  # 已取消/转移 → 停止（不再付费调用）
            operation_key = f"planning:{entry['chapter_id']}:{gi}"
            quota = sub_quotas[gi]
            fingerprint = group_fingerprint(group, quota, versions)
            units = _run_group(
                session,
                task,
                settings=settings,
                client=client,
                operation_key=operation_key,
                fingerprint=fingerprint,
                quota=quota,
                pages=group,
                chapter=entry,
                custom_requirements=config.get("custom_requirements"),
                versions=versions,
                now=now,
            )
            if units is None:
                skipped_groups += 1
            else:
                merged.extend((u, entry["chapter_id"]) for u in units)
    if task.status != "RUNNING":
        return  # Key 错误等内部失败已置 FAILED（或外部转移）→ 不再落最终事务
    # 4. 合并：跨组指纹去重 + 全局 priority（§6.2）
    final_units = _merge_units(merged)
    # 5. 空单元三分支（§6.4）
    if total_groups > 0 and skipped_groups == total_groups:
        _finish_planning_failed(
            session,
            task,
            error_code=ErrorCode.GENERATION_FAILED.value,
            now=now,
            skipped=skipped_groups,
        )
        return
    if not final_units:
        _finish_planning_empty(session, task, now=now)
        return
    _finish_planning_generating(session, task, units=final_units, skipped=skipped_groups, now=now)


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


def _run_group(
    session: Session,
    task: Task,
    *,
    settings: Settings,
    client: DeepSeekClient,
    operation_key: str,
    fingerprint: str,
    quota: dict[str, int],
    pages: list[TextChunk],
    chapter: dict[str, Any],
    custom_requirements: Any,
    versions: dict[str, str],
    now: str,
) -> list[dict[str, Any]] | None:
    """单组规划：恢复复用 → 预算 → 尝试循环。返回规范化 units；None = 组 SKIPPED 或停止。"""
    saved = find_success_result(
        session,
        task_id=task.task_id,
        stage=_PLANNING_STAGE,
        operation_key=operation_key,
        input_fingerprint=fingerprint,
    )
    if saved is not None:
        try:
            units = json.loads(saved)
            assert isinstance(units, list)
            return units  # 恢复复用（§6.2：同 key+fingerprint 的 SUCCESS 不重复调用）
        except (ValueError, TypeError, AssertionError):
            logger.warning(
                "task planning ledger result unreadable, re-planning group",
                extra={"task_id": task.task_id, "operation_key": operation_key},
            )
    budget = 1 + settings.planning_retry_limit
    if (
        attempt_count(
            session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
        )
        >= budget
    ):
        return None  # 预算耗尽（含 UNKNOWN）→ 组 SKIPPED（§6.3 预算不重置）
    allowed_page_ids = {p.chunk_id for p in pages}
    page_chars = {p.chunk_id: p.char_count for p in pages}
    system_prompt, user_prompt = _build_planner_prompts(
        chapter=chapter,
        quota=quota,
        pages=pages,
        settings=settings,
        custom_requirements=custom_requirements,
    )
    while True:
        session.refresh(task)
        if task.status != "RUNNING" or task.stage != _PLANNING_STAGE:
            return None  # 已取消/转移 → 立即停止，不得再付费调用
        attempt_no = (
            attempt_count(
                session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
            )
            + 1
        )
        attempt = create_attempt(
            session,
            device_id=task.device_id,
            scope_type="TASK",
            scope_id=task.task_id,
            task_id=task.task_id,
            stage=_PLANNING_STAGE,
            operation_key=operation_key,
            input_fingerprint=fingerprint,
            attempt_no=attempt_no,
            model=settings.deepseek_model,
            prompt_name="planner",
            prompt_version=versions["planner_prompt_version"],
            schema_name="planner_output",
            schema_version=versions["planner_output_schema_version"],
            now=now,
        )
        task.updated_at = now  # 心跳与 STARTED 占位同事务（§9 调用前先有已提交 STARTED 行）
        session.commit()
        try:
            result = client.chat(
                user_prompt, system_prompt=system_prompt, max_tokens=_PLANNER_MAX_OUTPUT_TOKENS
            )
        except RetryableUpstreamError as exc:
            if exc.code is ErrorCode.API_KEY_UNAVAILABLE and not exc.retryable:
                # Key 错误（401，§6.3）：任务 FAILED + PLANNING，不重试
                finish_failed(session, attempt, error_code=exc.code.value, now=now)
                task.updated_at = now
                session.commit()
                _fail_planning_inplace(task, error_code=exc.code.value, now=now)
                return None
            # 上游暂时失败（429/5xx/网络）与输出解析失败 → 预算内重试（§6.3）
            finish_failed(session, attempt, error_code=exc.code.value, now=now)
            task.updated_at = now
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
        except Exception:  # noqa: BLE001 —— 未预期异常按输出类失败走预算重试
            finish_failed(session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now)
            task.updated_at = now
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
        # 事务外校验（§6.3 输出非法 → 预算内重试；红线 4：原始响应不落库）
        try:
            raw = json.loads(result["content"])
            units = validate_and_truncate(
                raw,
                allowed_page_ids=allowed_page_ids,
                quota=quota,
                max_pages_per_unit=settings.max_source_pages_per_unit,
                max_chars_per_unit=settings.generator_max_input_chars,
                page_chars=page_chars,
            )
        except (ValueError, TypeError, AppError):
            finish_failed(session, attempt, error_code=ErrorCode.GENERATION_FAILED.value, now=now)
            task.updated_at = now
            session.commit()
            if _attempt_total(session, task, operation_key) >= budget:
                return None
            continue
        finish_success(
            session,
            attempt,
            usage=result["usage"],
            http_status=result["http_status"],
            duration_ms=result["duration_ms"],
            normalized_result=json.dumps(units, ensure_ascii=False),
            now=now,
        )
        task.updated_at = now
        session.commit()
        return units


def _attempt_total(session: Session, task: Task, operation_key: str) -> int:
    """本组尝试数（含全部状态；§9 预算口径）。"""
    return attempt_count(
        session, task_id=task.task_id, stage=_PLANNING_STAGE, operation_key=operation_key
    )


def _build_planner_prompts(
    *,
    chapter: dict[str, Any],
    quota: dict[str, int],
    pages: list[TextChunk],
    settings: Settings,
    custom_requirements: Any,
) -> tuple[str, str]:
    """Planner 双消息组装（spec §5.7）：稳定 system（prompt + schema 原文）+ 动态 user。"""
    system_prompt = (
        f"{load_asset('prompts', 'planner')}\n\n<PLANNER_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'planner_output')}\n</PLANNER_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {
            "chapter_id": chapter["chapter_id"],
            "name": chapter["name"],
            "start_page": chapter["start_page"],
            "end_page": chapter["end_page"],
        },
        "difficulty_quota": quota,
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
    """跨组去重（§6.2）：指纹 = (learning_objective, target_difficulty, card_type,
    page 序 source_chunk_ids)；按章序/组序/数组顺序保留首次出现，全局 priority 1..N。"""
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


def _fail_planning_inplace(task: Task, *, error_code: str, now: str) -> None:
    """Key 错误等即时失败：任务 FAILED + failure_stage=PLANNING（调用方 commit 落库）。"""
    task.status = "FAILED"
    task.failure_stage = _PLANNING_STAGE
    task.error_code = error_code
    task.ended_at = now
    task.resumable = 0
    logger.warning(
        "task planning failed",
        extra={"task_id": task.task_id, "error_code": error_code},
    )


def _planning_guard_update(session: Session, task: Task, *, values: dict[str, Any]) -> bool:
    """最终短事务条件更新（§6.2 step 7）：WHERE RUNNING+PLANNING；rowcount=0 → 回滚。"""
    session.refresh(task)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(Task)
            .where(
                Task.task_id == task.task_id,
                Task.status == "RUNNING",
                Task.stage == _PLANNING_STAGE,
            )
            .values(**values)
        ),
    )
    if result.rowcount == 0:
        session.rollback()  # 条件不成立（已取消/转移）→ 整事务回滚，不信任 identity map
        return False
    return True


def _finish_planning_failed(
    session: Session, task: Task, *, error_code: str, now: str, skipped: int
) -> None:
    """全部规划组失败（§6.4 分支 2）→ FAILED + failure_stage=PLANNING（条件更新）。"""
    if not _planning_guard_update(
        session,
        task,
        values={
            "status": "FAILED",
            "failure_stage": _PLANNING_STAGE,
            "error_code": error_code,
            "ended_at": now,
            "resumable": 0,
            "skipped_planning_group_count": skipped,
        },
    ):
        return
    task.status = "FAILED"
    task.failure_stage = _PLANNING_STAGE
    task.error_code = error_code
    task.ended_at = now
    task.resumable = 0
    task.skipped_planning_group_count = skipped
    logger.warning(
        "task planning failed",
        extra={
            "task_id": task.task_id,
            "error_code": error_code,
            "skipped_planning_group_count": skipped,
        },
    )


def _finish_planning_empty(session: Session, task: Task, *, now: str) -> None:
    """全组成功但 0 个合法单元（§6.4 分支 1）→ COMPLETED + NO_GENERATION_UNITS（条件更新）。"""
    if not _planning_guard_update(
        session,
        task,
        values={
            "status": "COMPLETED",
            "completion_reason": "NO_GENERATION_UNITS",
            "total_batch_count": 0,
            "completed_batch_count": 0,
            "ended_at": now,
            "resumable": 0,
            "updated_at": now,
        },
    ):
        return
    task.status = "COMPLETED"
    task.completion_reason = "NO_GENERATION_UNITS"
    task.total_batch_count = 0
    task.completed_batch_count = 0
    task.ended_at = now
    task.resumable = 0
    logger.info(
        "task planning empty result",
        extra={"task_id": task.task_id, "completion_reason": "NO_GENERATION_UNITS"},
    )


def _finish_planning_generating(
    session: Session, task: Task, *, units: list[dict[str, Any]], skipped: int, now: str
) -> None:
    """最终短事务（§6.2 step 7）：条件更新 → 写 KnowledgePoint + plan_batches +
    stage=GENERATING + skipped 计数 + 难度分布 cursor。rowcount=0 → 回滚返回。"""
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
                source_chunk_ids=json.dumps(chunk_ids, ensure_ascii=False),
            )
        )
    session.add_all(kps)
    session.flush()
    # plan_batches 已按新签名（1 单元 1 批 + generation_unit_id；spec §7）
    plan_batches(session, task_id=task.task_id, knowledge_points=kps)
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
    distribution = {"BASIC": 0, "UNDERSTANDING": 0, "APPLICATION": 0}
    for unit in units:
        distribution[unit["target_difficulty"]] += 1
    return distribution
