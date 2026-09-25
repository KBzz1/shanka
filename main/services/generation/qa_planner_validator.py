"""qa_planner_validator.py：问答直通规划输出校验与规范化（V25-D-43；spec §5.6 分层）。

分层校验（schema 负责结构，代码负责来源接地与上限）：

- Schema：``load_schema_asset("qa_planner_output")``（v1）负责根包装、QUESTION /
  TRUE_FALSE 双形态结构、必填、范围与禁额外键；违反 → AppError(GENERATION_FAILED)
  （§6.3 输出非法）。
- 代码层逐对校验：``source_chunk_ids`` ⊆ 本次调用页集合；引用块数 ≤ max_chunks_per_pair、
  来源字符和 ≤ max_chars_per_pair；违反 → AppError(GENERATION_FAILED)。
- 规范化：``source_chunk_ids`` 按页序重排、去重；去重键用 ``normalize_question``
  （空白折叠 + 题号前缀剥离）由调用方在跨段合并时使用。

输出经济（v1 修订）：planner 输出是**清单**——只有题干/陈述、卡型与出处，不回抄答案
（答案由制卡阶段按出处回原文照录）。校验前先剥除模型仍可能回抄的 answer /
answer_boolean / explanation 键（防御性容忍，不因多余键烧重试预算；缺失的题干/出处
仍按 Schema 拒绝）。

配额说明：问答直通无难度/覆盖配额（资料问答对数量即单元数量，PRD V25-D-43）；
仅任务级单元硬上限（max_generation_units_per_task）在合并层截断，不重试。
"""

import re
from typing import Any

import jsonschema

from app.errors import AppError, ErrorCode
from infra.llm.prompts import load_schema_asset

_QUESTION_NOISE_PREFIX = re.compile(
    r"^(?:[0-9]{1,3}\s*[.、)．]"
    r"|第\s*[0-9一二三四五六七八九十百]+\s*[题条]?\s*[.、:：]"
    r"|[Qq][0-9]{1,3}\s*[:.．]"
    r"|[（(]\s*[0-9一二三四五六七八九十]{1,3}\s*[)）])\s*"
)


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.GENERATION_FAILED, message)


def normalize_question(text: str) -> str:
    """问答去重键：剥离题号/序号前缀 + 折叠全部空白（跨段重复问答判定，§6.2 合并）。"""
    stripped = _QUESTION_NOISE_PREFIX.sub("", text.strip())
    return "".join(stripped.split())


def validate_and_normalize_pairs(
    raw: dict[str, Any],
    *,
    allowed_page_ids: set[str],
    max_chunks_per_pair: int,
    max_chars_per_pair: int,
    page_chars: dict[str, int],
) -> list[dict[str, Any]]:
    """问答直通原始输出 → 规范化问答对列表（source_chunk_ids 页序去重）。

    page_chars 契约：``{chunk_id: char_count}``，调用方必须按 ``load_pages`` 页序
    （page_number 升序）构造——规范化按该插入序重排来源（与 planner_validator 同款）。
    """
    schema = load_schema_asset("qa_planner_output")
    pairs = _schema_validate(_strip_stale_answer_keys(raw), schema)
    for pair in pairs:
        _check_pair(
            pair,
            allowed_page_ids=allowed_page_ids,
            max_chunks_per_pair=max_chunks_per_pair,
            max_chars_per_pair=max_chars_per_pair,
            page_chars=page_chars,
        )
    page_order = {chunk_id: i for i, chunk_id in enumerate(page_chars)}
    return _normalize(pairs, page_order=page_order)


def _strip_stale_answer_keys(raw: dict[str, Any]) -> dict[str, Any]:
    """剥除模型回抄的答案类键（输出经济容忍）：清单契约下这些字段本不该出现。"""
    stale = ("answer", "answer_boolean", "explanation")
    pairs = raw.get("qa_pairs")
    if not isinstance(pairs, list):
        return raw
    cleaned = [
        {k: v for k, v in pair.items() if k not in stale} if isinstance(pair, dict) else pair
        for pair in pairs
    ]
    return {**raw, "qa_pairs": cleaned}


def _schema_validate(raw: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Schema 校验（qa-planner-output v1：{qa_pairs:[...]} 双形态，禁额外键）。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("qa_pairs"), list):
        raise _invalid("qa-planner 输出结构非法（顶层必须为含 qa_pairs 数组的对象）")
    errors = [
        f"{err.json_path or err.path}: {err.message}"
        for err in jsonschema.Draft202012Validator(schema).iter_errors(raw)
    ]
    if errors:
        raise _invalid("qa-planner 输出不满足输出 Schema")
    return [p for p in raw["qa_pairs"] if isinstance(p, dict)]


def _check_pair(
    pair: dict[str, Any],
    *,
    allowed_page_ids: set[str],
    max_chunks_per_pair: int,
    max_chars_per_pair: int,
    page_chars: dict[str, int],
) -> None:
    """代码层逐对校验：来源子集、块数上限、字符和上限。"""
    chunk_ids = pair["source_chunk_ids"]
    if any(cid not in allowed_page_ids for cid in chunk_ids):
        raise _invalid("qa-planner 来源引用超出本次调用页集合")
    if len(chunk_ids) > max_chunks_per_pair:
        raise _invalid("qa-planner 问答对来源块数超出上限")
    if sum(page_chars.get(cid, 0) for cid in chunk_ids) > max_chars_per_pair:
        raise _invalid("qa-planner 问答对来源字符数超出上限")


def _normalize(pairs: list[dict[str, Any]], *, page_order: dict[str, int]) -> list[dict[str, Any]]:
    """规范化：source_chunk_ids 按页序重排 + 去重；其余字段照录（忠实语义：不改内容）。"""
    normalized: list[dict[str, Any]] = []
    for pair in pairs:
        chunk_ids = list(
            dict.fromkeys(
                sorted(
                    pair["source_chunk_ids"],
                    key=lambda cid: page_order.get(cid, len(page_order)),
                )
            )
        )
        normalized.append({**pair, "source_chunk_ids": chunk_ids})
    return normalized
