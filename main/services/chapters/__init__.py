"""services.chapters：AI 章节边界规划（V25-D-36）。

资料类型中立的章节结构发现层：输入 text_chunks 块文本序列，输出章节区间；
本期由 PDF 扫描器对无目录资料接线，TEXT/ZIP 推广时只换触发条件与输入来源。
"""

from services.chapters.planner import ChapterPlan, plan_chapters, split_segments
from services.chapters.validator import (
    ChapterBoundary,
    normalize_title,
    validate_boundaries,
)

__all__ = [
    "ChapterBoundary",
    "ChapterPlan",
    "normalize_title",
    "plan_chapters",
    "split_segments",
    "validate_boundaries",
]
