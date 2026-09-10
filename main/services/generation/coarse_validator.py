"""coarse_validator.py：粗规划输出校验与规范化（V2.5.2 两阶段规划，spec §5.6 分层）。

分层校验（schema 负责结构，代码负责来源/层级/数量）：

- Schema：`load_schema_asset("planner_coarse_output")`（v7）负责根包装、必填、枚举与
  禁额外键；违反 → AppError(GENERATION_FAILED)（§6.3 输出非法，走预算内重试）。
- 代码层确定性过滤（违规主题丢弃并计数，不重试——模型违规按"少而正确"处理）：
  - `source_chunk_ids` ⊆ 本章页集合；
  - 页数 ≤ max_chunks_per_topic、来源字符和 ≤ max_chars_per_topic；
  - `coverage_tier` ∈ coverage_mode 允许集（COMPACT→CORE；BALANCED→+IMPORTANT；
    EXTENSIVE→全三级）——档位语义的结构保证，不依赖模型自觉；
  - 标题规范化（去空白/标点 + 小写）后去重，保留首次出现。
- 数量截断：主题数 > 区间上限 → 按数组序保留前 max 个（确定性）。
- 规范化：`topic_index` 服务端分配 1..N（模型不输出）。

红线 4：只保存通过校验的规范化 topics JSON，不保存完整 Prompt、原文或原始响应。
"""

import logging
import re
from typing import Any, cast

import jsonschema

from app.errors import AppError, ErrorCode
from infra.llm.prompts import load_schema_asset

logger = logging.getLogger(__name__)

TIER_ALLOWED: dict[str, set[str]] = {
    "COMPACT": {"CORE"},
    "BALANCED": {"CORE", "IMPORTANT"},
    "EXTENSIVE": {"CORE", "IMPORTANT", "LOW_FREQUENCY"},
}

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!()（）【】\[\]《》<>\"'‘’“”·…\-—_/\\|]+")


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.GENERATION_FAILED, message)


def normalize_title(title: str) -> str:
    """标题规范化：去空白与标点、拉丁转小写——确定性去重键。"""
    return _PUNCT.sub("", title).lower()


def validate_and_normalize_topics(
    raw: dict[str, Any],
    *,
    coverage_mode: str,
    topic_interval: tuple[int, int],
    allowed_page_ids: set[str],
    max_chunks_per_topic: int,
    max_chars_per_topic: int,
    page_chars: dict[str, int],
) -> list[dict[str, Any]]:
    """粗规划原始输出 → 规范化且过滤截断后的 topics（topic_index 1..N）。

    page_chars 契约：`{chunk_id: char_count}`，调用方按 `load_pages` 页序构造。
    """
    schema = load_schema_asset("planner_coarse_output")
    topics_raw = _schema_validate(raw, schema)
    allowed_tiers = TIER_ALLOWED.get(coverage_mode)
    if allowed_tiers is None:
        raise _invalid("非法 coverage_mode")
    seen_titles: set[str] = set()
    kept: list[dict[str, Any]] = []
    dropped = {"unknown_chunk": 0, "limits": 0, "tier": 0, "dup_title": 0}
    for topic in topics_raw:
        ids = cast(list[str], topic["source_chunk_ids"])
        if any(cid not in allowed_page_ids for cid in ids):
            dropped["unknown_chunk"] += 1
            continue
        if (
            len(ids) > max_chunks_per_topic
            or sum(page_chars.get(cid, 0) for cid in ids) > max_chars_per_topic
        ):
            dropped["limits"] += 1
            continue
        if topic["coverage_tier"] not in allowed_tiers:
            dropped["tier"] += 1
            continue
        norm = normalize_title(cast(str, topic["title"]))
        if not norm or norm in seen_titles:
            dropped["dup_title"] += 1
            continue
        seen_titles.add(norm)
        kept.append(
            {
                "title": topic["title"],
                "coverage_tier": topic["coverage_tier"],
                "source_chunk_ids": list(ids),
            }
        )
    upper = topic_interval[1]
    if len(kept) > upper:
        kept = kept[:upper]
    for index, topic in enumerate(kept, start=1):
        topic["topic_index"] = index
    if any(dropped.values()):
        logger.info(
            "coarse topics filtered",
            extra={"coverage_mode": coverage_mode, **dropped},
        )
    return kept


def _schema_validate(raw: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Schema 校验：顶层必须为含 topics 数组的对象，逐主题禁额外键。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("topics"), list):
        raise _invalid("粗规划输出结构非法（顶层必须为含 topics 数组的对象）")
    errors = [
        f"{err.json_path or err.path}: {err.message}"
        for err in jsonschema.Draft202012Validator(schema).iter_errors(raw)
    ]
    if errors:
        raise _invalid("粗规划输出不满足输出 Schema")
    return [t for t in raw["topics"] if isinstance(t, dict)]
