"""planner.py：AI 章节边界规划（V25-D-36，无目录 PDF；资料类型中立）。

编排（仿 planning_executor 的账本定式，轻量独立实现，不动任务规划生产代码）：

- 输入 = ``text_chunks`` 块文本序列（PDF=真页码、TEXT/ZIP=伪页码，调用方决定来源）；
- 分段：按 ``ai_chapter_max_input_chars`` 连续块贪心分段（块文本不切分、单块超限独立
  成段）；段数超 ``ai_chapter_max_segments`` → AppError(PDF_AI_CHAPTERS_FAILED)；
- 每段一次 LLM 调用（system = chapter_planner v9 prompt + schema 原文，**各段逐字节
  一致**以吃 DeepSeek 自动前缀缓存；user = XML 信封段内页），只识别"在本段内开始"的
  章节边界；确定性校验见 validator.py；
- 账本：``llm_call_attempts`` stage='CHAPTER_PLANNING'、scope_type='MATERIAL'、
  scope_id=material_id、task_id=NULL；operation_key=``chapters:{material_id}:{seg}``；
  同 key+fingerprint 的 SUCCESS 恢复复用（重试/重启不重复付费）；预算
  ``ai_chapter_retry_limit``；跨请求幂等以账本为权威（不建 GenerationOperation——
  该表挂任务语义，章节规划无任务）；
- 合并：全段边界按页排序去重（页码 + 规范化标题双键）→ 首边界非起始页补「开篇」→
  区间归一化（end = 下一 start − 1，同 parse_pdf 语义）；
- 退化：0 有效边界 → 整本单章（name=资料名），静默降级不失败。

失败语义：段预算耗尽 / 输出持续非法 / 上游不可恢复错误 → AppError(PDF_AI_CHAPTERS_FAILED)
（调用方落 FAILED，用户可 reparse 或整本降级）；租约丢失（on_segment_done 返回 False）→
``LeaseLost`` 内部异常，调用方丢弃结果、不改状态（并发接管/删除的栅栏语义）。

红线 4：明文 API Key 只在 client 实例内；normalized_result 只存规范化边界 JSON，
不保存完整 Prompt、原文或原始模型响应。
"""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import LlmCallAttempt, TextChunk
from infra.db.session import format_utc
from infra.llm.deepseek import LlmChatClient, RetryableUpstreamError
from infra.llm.prompts import asset_versions, load_asset, safe_json_dumps
from services.chapters.validator import (
    ChapterBoundary,
    normalize_title,
    validate_boundaries,
)
from services.generation.ledger import create_attempt, finish_failed, finish_success

logger = logging.getLogger(__name__)

_STAGE = "CHAPTER_PLANNING"
_SCOPE_TYPE = "MATERIAL"

# 0 边界引导重试的附加指令（追加在信封之后，服务端→模型指令；v10 分层原理的具体化）
_GUIDED_RETRY_SUFFIX = (
    "\n\n上一次分析未识别任何章节边界。请按「章节体系判定原理」重新检查本次输入：\n"
    "1. 文档中是否存在一致的编号标题体系（中文序号「一/二/三」、「第 N 章」、"
    '"Part N"、「N.」等任意形式）？若存在，编号跨度最大、重复频次最低的那一级'
    "就是章节体系，其每个成员标题行所在页都是章节边界，必须输出；\n"
    "2. 高频重复的编号行（题号/卡片号/练习号）是条目，不是章节边界；\n"
    "3. 仅当确实不存在任何编号标题体系、也没有可信的无编号主题切换时，才允许输出空数组。"
)


class ChapterPlan(TypedDict):
    """章节区间（页码闭区间，1-based 语义由调用方的块序号定义）。"""

    name: str
    start_page: int
    end_page: int


class LeaseLost(Exception):
    """调用方租约/栅栏失效（on_segment_done 返回 False）：丢弃结果，不改状态。"""


def _now_utc() -> str:
    return format_utc(SystemClock().now_utc())


def _fail(message: str) -> AppError:
    return AppError(ErrorCode.PDF_AI_CHAPTERS_FAILED, message)


def split_segments(chunks: Sequence[TextChunk], *, max_chars: int) -> list[list[TextChunk]]:
    """连续块贪心分段：块文本不切分，累计 char_count 超 max_chars 开新段；单块超限独立成段。"""
    segments: list[list[TextChunk]] = []
    current: list[TextChunk] = []
    current_chars = 0
    for chunk in chunks:
        if current and current_chars + chunk.char_count > max_chars:
            segments.append(current)
            current, current_chars = [], 0
        current.append(chunk)
        current_chars += chunk.char_count
    if current:
        segments.append(current)
    return segments


def merge_boundaries(
    boundaries: Sequence[ChapterBoundary],
    *,
    first_page: int,
    last_page: int,
    material_name: str,
) -> list[ChapterPlan]:
    """全段边界 → 章节区间：页码升序去重（页 + 规范化标题双键）→ 补「开篇」→ 区间归一化。

    0 有效边界 → 整本单章（name=资料名，静默降级）。
    """
    seen_pages: set[int] = set()
    seen_titles: set[str] = set()
    marks: list[ChapterBoundary] = []
    for boundary in sorted(boundaries, key=lambda b: b["start_page"]):
        page = boundary["start_page"]
        norm = normalize_title(boundary["title"])
        if page in seen_pages or not norm or norm in seen_titles:
            continue
        seen_pages.add(page)
        seen_titles.add(norm)
        marks.append(boundary)
    if not marks:
        return [ChapterPlan(name=material_name, start_page=first_page, end_page=last_page)]
    if marks[0]["start_page"] != first_page:
        marks.insert(0, ChapterBoundary(title="开篇", start_page=first_page))
    chapters: list[ChapterPlan] = []
    for index, mark in enumerate(marks):
        start = mark["start_page"]
        end = marks[index + 1]["start_page"] - 1 if index + 1 < len(marks) else last_page
        chapters.append(ChapterPlan(name=mark["title"], start_page=start, end_page=max(start, end)))
    return chapters


def _segment_fingerprint(material_id: str, seg_index: int, segment: Sequence[TextChunk]) -> str:
    raw = "|".join(f"{c.page_number}:{c.content_sha256}" for c in segment)
    return hashlib.sha256(f"{material_id}:{seg_index}:{raw}".encode()).hexdigest()


def _attempt_total(session: Session, *, scope_id: str, operation_key: str) -> int:
    """本操作尝试数（scope 口径；四种状态全部计为已尝试，ledger 预算同款）。"""
    rows = session.execute(
        select(LlmCallAttempt.attempt_no).where(
            LlmCallAttempt.scope_type == _SCOPE_TYPE,
            LlmCallAttempt.scope_id == scope_id,
            LlmCallAttempt.stage == _STAGE,
            LlmCallAttempt.operation_key == operation_key,
        )
    )
    return max((int(no) for (no,) in rows), default=0)


def _saved_boundaries(
    session: Session, *, scope_id: str, operation_key: str, fingerprint: str
) -> list[ChapterBoundary] | None:
    """恢复复用：同 key+fingerprint 的 SUCCESS normalized_result（不重复付费）。"""
    saved = session.scalar(
        select(LlmCallAttempt.normalized_result)
        .where(
            LlmCallAttempt.scope_type == _SCOPE_TYPE,
            LlmCallAttempt.scope_id == scope_id,
            LlmCallAttempt.stage == _STAGE,
            LlmCallAttempt.operation_key == operation_key,
            LlmCallAttempt.input_fingerprint == fingerprint,
            LlmCallAttempt.status == "SUCCESS",
        )
        .limit(1)
    )
    if saved is None:
        return None
    try:
        parsed = json.loads(saved)
        assert isinstance(parsed, list)
        return [
            ChapterBoundary(title=str(b["title"]), start_page=int(b["start_page"])) for b in parsed
        ]
    except (ValueError, TypeError, KeyError, AssertionError):
        logger.warning(
            "chapter planning ledger result unreadable, re-planning segment",
            extra={"operation_key": operation_key},
        )
        return None


def _run_segment_operation(
    session: Session,
    *,
    user_id: str,
    material_id: str,
    seg_index: int,
    segment: Sequence[TextChunk],
    system_prompt: str,
    user_prompt: str,
    fingerprint: str,
    settings: Settings,
    client: LlmChatClient,
    prompt_version: str,
    schema_version: str,
    operation_key_suffix: str = "",
) -> list[ChapterBoundary]:
    """单段调用：恢复复用 → 预算 → 尝试循环（STARTED 占位 → 事务外 chat → 事务外校验）。

    ``operation_key_suffix``（如 ``":guided"``）区分同段的不同轮次（0 边界引导重试），
    账本唯一键随之独立、互不挤占预算；fingerprint 由调用方带同款后缀区分输入。
    """
    operation_key = f"chapters:{material_id}:{seg_index}{operation_key_suffix}"
    saved = _saved_boundaries(
        session, scope_id=material_id, operation_key=operation_key, fingerprint=fingerprint
    )
    if saved is not None:
        return saved
    budget = 1 + settings.ai_chapter_retry_limit
    if _attempt_total(session, scope_id=material_id, operation_key=operation_key) >= budget:
        raise _fail(f"第 {seg_index + 1} 段章节规划预算耗尽（可重试或按整本继续）")
    while True:
        attempt_no = _attempt_total(session, scope_id=material_id, operation_key=operation_key) + 1
        attempt = create_attempt(
            session,
            user_id=user_id,
            scope_type=_SCOPE_TYPE,
            scope_id=material_id,
            task_id=None,
            stage=_STAGE,
            operation_key=operation_key,
            input_fingerprint=fingerprint,
            attempt_no=attempt_no,
            model=settings.deepseek_model,
            prompt_name="chapter_planner",
            prompt_version=prompt_version,
            schema_name="chapter_planner_output",
            schema_version=schema_version,
            now=_now_utc(),
        )
        session.commit()
        try:
            result = client.chat(
                user_prompt,
                system_prompt=system_prompt,
                max_tokens=settings.ai_chapter_max_output_tokens,
            )
        except RetryableUpstreamError as exc:
            finish_failed(session, attempt, error_code=exc.code.value, now=_now_utc())
            session.commit()
            if not exc.retryable:
                raise _fail("AI 章节规划失败（API Key 不可用），可重试或按整本继续") from None
            if _attempt_total(session, scope_id=material_id, operation_key=operation_key) >= budget:
                raise _fail(f"第 {seg_index + 1} 段章节规划失败（上游持续不可用）") from None
            continue
        except Exception:  # noqa: BLE001 —— 未预期异常按输出类失败走预算重试
            finish_failed(
                session, attempt, error_code=ErrorCode.PDF_AI_CHAPTERS_FAILED.value, now=_now_utc()
            )
            session.commit()
            if _attempt_total(session, scope_id=material_id, operation_key=operation_key) >= budget:
                raise _fail(f"第 {seg_index + 1} 段章节规划失败（调用异常）") from None
            continue
        try:  # 事务外校验（红线 4：原始响应不落库）
            raw = json.loads(result["content"])
            boundaries = validate_boundaries(
                raw,
                segment_start=segment[0].page_number,
                segment_end=segment[-1].page_number,
                max_boundaries=settings.ai_chapter_max_boundaries_per_segment,
            )
        except (ValueError, TypeError, AppError):
            finish_failed(
                session, attempt, error_code=ErrorCode.PDF_AI_CHAPTERS_FAILED.value, now=_now_utc()
            )
            session.commit()
            if _attempt_total(session, scope_id=material_id, operation_key=operation_key) >= budget:
                raise _fail(f"第 {seg_index + 1} 段章节规划输出非法（预算耗尽）") from None
            continue
        finish_success(
            session,
            attempt,
            usage=result["usage"],
            http_status=result["http_status"],
            duration_ms=result["duration_ms"],
            normalized_result=json.dumps(boundaries, ensure_ascii=False),
            now=_now_utc(),
        )
        session.commit()
        return boundaries


def build_segment_prompts(
    *,
    material_name: str,
    segment: Sequence[TextChunk],
    first_page: int,
    last_page: int,
    settings: Settings,
) -> tuple[str, str]:
    """单段双消息组装（评测 harness 与生产共用，保证口径逐字节一致）。

    system 各段逐字节一致（自动前缀缓存命中）；user = XML 信封段内页 JSON。
    """
    system_prompt = (
        f"{load_asset('prompts', 'chapter_planner')}\n\n<CHAPTER_PLANNER_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'chapter_planner_output')}\n</CHAPTER_PLANNER_OUTPUT_SCHEMA>"
    )
    payload: dict[str, Any] = {
        "material_name": material_name,
        "total_pages": last_page - first_page + 1,
        "segment": {
            "start_page": segment[0].page_number,
            "end_page": segment[-1].page_number,
        },
        "pages": [{"page_number": p.page_number, "content": p.content} for p in segment],
        "limits": {"max_boundaries_per_segment": settings.ai_chapter_max_boundaries_per_segment},
    }
    user_prompt = f"<CHAPTER_PLANNER_INPUT>{safe_json_dumps(payload)}</CHAPTER_PLANNER_INPUT>"
    return system_prompt, user_prompt


def plan_chapters(
    session: Session,
    *,
    user_id: str,
    material_id: str,
    material_name: str,
    chunks: Sequence[TextChunk],
    client: LlmChatClient,
    settings: Settings,
    on_segment_done: Callable[[], bool] | None = None,
) -> list[ChapterPlan]:
    """整份资料的块文本 → 章节区间列表（source 由调用方落库为 AI）。

    ``on_segment_done``：每段完成后的租约刷新回调（返回 False = 栅栏失效 → LeaseLost，
    调用方丢弃结果）。段数超上限 / 段预算耗尽 / 输出持续非法 → AppError(PDF_AI_CHAPTERS_FAILED)。
    """
    if not chunks:
        raise _fail("资料无文本块，无法规划章节")
    ordered = sorted(chunks, key=lambda c: c.page_number)
    segments = split_segments(ordered, max_chars=settings.ai_chapter_max_input_chars)
    if len(segments) > settings.ai_chapter_max_segments:
        raise _fail(
            f"文件过大（{len(segments)} 段超过 {settings.ai_chapter_max_segments} 段上限），"
            "请按整本继续或拆分文件"
        )
    versions = asset_versions()
    first_page = ordered[0].page_number
    last_page = ordered[-1].page_number
    boundaries: list[ChapterBoundary] = []
    for seg_index, segment in enumerate(segments):
        system_prompt, user_prompt = build_segment_prompts(
            material_name=material_name,
            segment=segment,
            first_page=first_page,
            last_page=last_page,
            settings=settings,
        )
        seg_boundaries = _run_segment_operation(
            session,
            user_id=user_id,
            material_id=material_id,
            seg_index=seg_index,
            segment=segment,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            fingerprint=_segment_fingerprint(material_id, seg_index, segment),
            settings=settings,
            client=client,
            prompt_version=versions["chapter_planner_prompt_version"],
            schema_version=versions["chapter_planner_output_schema_version"],
        )
        if not seg_boundaries:
            # 0 边界引导重试（V25-D-36 流程韧性）：静默全弃对用户不可见且最伤产品
            # （陌生形态文档整本降级）。换引导版指令重试一次，独立 operation_key
            # 不挤占首次预算；仍为空才视为"该段确实无章节结构"。
            guided_prompt = user_prompt + _GUIDED_RETRY_SUFFIX
            seg_boundaries = _run_segment_operation(
                session,
                user_id=user_id,
                material_id=material_id,
                seg_index=seg_index,
                segment=segment,
                system_prompt=system_prompt,
                user_prompt=guided_prompt,
                fingerprint=_segment_fingerprint(material_id, seg_index, segment) + ":guided",
                settings=settings,
                client=client,
                prompt_version=versions["chapter_planner_prompt_version"],
                schema_version=versions["chapter_planner_output_schema_version"],
                operation_key_suffix=":guided",
            )
        boundaries.extend(seg_boundaries)
        if on_segment_done is not None and not on_segment_done():
            raise LeaseLost(f"{material_id} 解析租约失效，丢弃章节规划结果")
    return merge_boundaries(
        boundaries,
        first_page=first_page,
        last_page=last_page,
        material_name=material_name,
    )
