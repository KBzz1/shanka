"""services.chapters.planner/validator 纯逻辑单元测试（V25-D-36）。

覆盖：贪心分段、边界合并（开篇补齐/双键去重/区间归一化/零边界退化）、
确定性校验（段内页码/标题去重/数量截断/Schema 非法）。
账本与扫描器接线（租约/恢复复用/预算）见 tests/integration/test_pdf_scanner.py。
命名规范：test_<模块>_<行为>。
"""

import pytest

from app.errors import AppError, ErrorCode
from infra.db.models import TextChunk
from services.chapters.planner import merge_boundaries, split_segments
from services.chapters.validator import ChapterBoundary, validate_boundaries


def _chunk(page: int, chars: int = 100, content: str | None = None) -> TextChunk:
    return TextChunk(
        chunk_id=f"c{page}",
        material_id="m",
        chunk_seq=page,
        page_number=page,
        char_count=chars,
        content=content or "x" * chars,
        content_sha256=f"sha{page}",
        created_at="2026-09-16T00:00:00.000Z",
    )


def test_chapter_planner_split_segments_greedy() -> None:
    """连续块贪心分段：累计超限开新段；块不切分、单块超限独立成段。"""
    chunks = [_chunk(1, 60), _chunk(2, 60), _chunk(3, 10), _chunk(4, 500), _chunk(5, 5)]
    segments = split_segments(chunks, max_chars=100)
    assert [[c.page_number for c in seg] for seg in segments] == [[1], [2, 3], [4], [5]]


def test_chapter_planner_split_segments_single_chunk_within_limit() -> None:
    chunks = [_chunk(1, 40), _chunk(2, 40)]
    segments = split_segments(chunks, max_chars=100)
    assert len(segments) == 1  # 全部块合并一段


def test_chapter_planner_merge_prepends_intro() -> None:
    """首边界非起始页 → 自动补「开篇」章。"""
    plans = merge_boundaries(
        [ChapterBoundary(title="第 1 章 绪论", start_page=9)],
        first_page=1,
        last_page=100,
        material_name="书",
    )
    assert [p["name"] for p in plans] == ["开篇", "第 1 章 绪论"]
    assert (plans[0]["start_page"], plans[0]["end_page"]) == (1, 8)
    assert (plans[1]["start_page"], plans[1]["end_page"]) == (9, 100)


def test_chapter_planner_merge_dedups_page_and_title() -> None:
    """跨段去重：同页码保留首个；规范化标题相同（标点/空白/大小写）保留首个。"""
    plans = merge_boundaries(
        [
            ChapterBoundary(title="第 2 章 检索", start_page=30),
            ChapterBoundary(title="第 2 章 检索", start_page=31),  # 同名不同页 → 丢
            ChapterBoundary(title="第2章检索", start_page=32),  # 规范化后同名 → 丢
            ChapterBoundary(title="第 3 章 生成", start_page=60),
            ChapterBoundary(title="第 1 章 绪论", start_page=1),
        ],
        first_page=1,
        last_page=99,
        material_name="书",
    )
    assert [p["name"] for p in plans] == ["第 1 章 绪论", "第 2 章 检索", "第 3 章 生成"]
    assert (plans[1]["start_page"], plans[1]["end_page"]) == (30, 59)


def test_chapter_planner_merge_zero_boundaries_degrades() -> None:
    """0 有效边界 → 整本单章（name=资料名），静默降级。"""
    plans = merge_boundaries([], first_page=1, last_page=42, material_name="我的书")
    assert plans == [{"name": "我的书", "start_page": 1, "end_page": 42}]


def test_chapter_planner_validator_drops_out_of_segment_pages() -> None:
    """页码越界（幻觉/跨段）丢弃；段内页码保留（页码 0 属 Schema 违约 → 整包拒绝）。"""
    kept = validate_boundaries(
        {
            "chapters": [
                {"title": "第 1 章", "start_page": 5},  # 段内
                {"title": "第 2 章", "start_page": 3},  # 段前 → 丢
                {"title": "第 3 章", "start_page": 999},  # 段后（幻觉）→ 丢
            ]
        },
        segment_start=5,
        segment_end=20,
        max_boundaries=10,
    )
    assert kept == [{"title": "第 1 章", "start_page": 5}]


def test_chapter_planner_validator_caps_and_dedups() -> None:
    """同段同页/同名去重 + 数量截断（按数组序确定性保留前 N）。"""
    kept = validate_boundaries(
        {
            "chapters": [
                {"title": "第 1 章", "start_page": 5},
                {"title": "第 1 章", "start_page": 5},  # 同页同名 → 丢
                {"title": "第 2 章", "start_page": 8},
                {"title": "第 3 章", "start_page": 12},
                {"title": "第 4 章", "start_page": 15},
            ]
        },
        segment_start=5,
        segment_end=20,
        max_boundaries=2,
    )
    assert [b["start_page"] for b in kept] == [5, 8]


def test_chapter_planner_validator_rejects_bad_schema() -> None:
    """结构/Schema 非法 → AppError(PDF_AI_CHAPTERS_FAILED)（预算内重试由调用方处理）。"""
    with pytest.raises(AppError) as excinfo:
        validate_boundaries(
            {"chapters": [{"title": "缺页码"}]},
            segment_start=1,
            segment_end=10,
            max_boundaries=5,
        )
    assert excinfo.value.code is ErrorCode.PDF_AI_CHAPTERS_FAILED
    with pytest.raises(AppError):
        validate_boundaries(
            {"unexpected": True},
            segment_start=1,
            segment_end=10,
            max_boundaries=5,
        )
