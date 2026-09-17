"""validator.py：章节边界规划输出的确定性校验与规范化（V25-D-36）。

分层校验（coarse_validator 同款分层：schema 管结构，代码管边界）：

- Schema：``load_schema_asset("chapter_planner_output")``（v9）负责根包装、必填与
  禁额外键；违反 → AppError(PDF_AI_CHAPTERS_FAILED)（预算内重试）。
- 代码层确定性过滤（违规边界丢弃，不重试——模型违规按"少而正确"处理）：
  - ``start_page`` 必须落在该段页码闭区间内（防幻觉页码/跨段越界）；
  - 标题规范化（去空白/标点 + 小写）后去重，保留首次出现；
  - 数量截断：超过 ``max_boundaries`` → 按数组序保留前 N 个（确定性）。

红线 4：只返回规范化边界列表，不保存完整 Prompt、原文或原始模型响应。
"""

import re
from typing import Any, TypedDict, cast

import jsonschema

from app.errors import AppError, ErrorCode
from infra.llm.prompts import load_schema_asset

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!()（）【】\[\]《》<>\"'‘’“”·…\-—_/\\|]+")


class ChapterBoundary(TypedDict):
    """单段识别出的章节边界（起始点，非区间；校验层拥有形状，规划器复用）。"""

    title: str
    start_page: int


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.PDF_AI_CHAPTERS_FAILED, message)


def normalize_title(title: str) -> str:
    """标题规范化：去空白与标点、拉丁转小写——确定性去重键（coarse 同款）。"""
    return _PUNCT.sub("", title).lower()


def validate_boundaries(
    raw: dict[str, Any],
    *,
    segment_start: int,
    segment_end: int,
    max_boundaries: int,
) -> list[ChapterBoundary]:
    """单段原始输出 → 规范化且过滤截断后的边界列表 ``[{"title", "start_page"}]``。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("chapters"), list):
        raise _invalid("章节规划输出结构非法（顶层必须为含 chapters 数组的对象）")
    schema = load_schema_asset("chapter_planner_output")
    errors = [
        f"{err.json_path or err.path}: {err.message}"
        for err in jsonschema.Draft202012Validator(schema).iter_errors(raw)
    ]
    if errors:
        raise _invalid("章节规划输出不满足输出 Schema")
    seen_pages: set[int] = set()
    seen_titles: set[str] = set()
    kept: list[ChapterBoundary] = []
    for boundary in cast(list[dict[str, Any]], raw["chapters"]):
        page = int(boundary["start_page"])
        if not (segment_start <= page <= segment_end):
            continue  # 页码越界（幻觉/跨段）：丢弃
        if page in seen_pages:
            continue
        norm = normalize_title(cast(str, boundary["title"]))
        if not norm or norm in seen_titles:
            continue
        seen_pages.add(page)
        seen_titles.add(norm)
        kept.append(ChapterBoundary(title=cast(str, boundary["title"]).strip(), start_page=page))
        if len(kept) >= max_boundaries:
            break
    return kept
