"""coarse_validator 测试（V2.5.2 两阶段规划）。

- Schema（planner-coarse-output v7）负责根包装/必填/枚举/禁额外键；
- 代码层确定性过滤：未知 chunk 引用 / 超 limits / tier 不在模式允许集 / 标题规范化去重；
- 数量截断到区间上限（按数组序）；topic_index 服务端分配 1..N。
"""

from typing import Any

import pytest

from app.errors import AppError, ErrorCode
from services.generation.coarse_validator import normalize_title, validate_and_normalize_topics

_INTERVAL = (3, 5)
_ARGS: dict[str, Any] = {
    "allowed_page_ids": {"ch1", "ch2", "ch3"},
    "max_chunks_per_topic": 2,
    "max_chars_per_topic": 85,
    "page_chars": {"ch1": 30, "ch2": 40, "ch3": 50},
}


def test_normalize_topics_happy_path() -> None:
    raw = {
        "topics": [
            {"title": "主题一", "coverage_tier": "CORE", "source_chunk_ids": ["ch1", "ch2"]},
            {"title": "主题二", "coverage_tier": "CORE", "source_chunk_ids": ["ch3"]},
        ]
    }
    out = validate_and_normalize_topics(
        raw, coverage_mode="COMPACT", topic_interval=_INTERVAL, **_ARGS
    )
    assert [t["topic_index"] for t in out] == [1, 2]
    assert out[0]["coverage_tier"] == "CORE"


def test_filters_disallowed_tiers_by_mode() -> None:
    """COMPACT 混入 IMPORTANT/LOW_FREQUENCY → 确定性过滤（档位语义结构保证）。"""
    raw = {
        "topics": [
            {"title": "主题一", "coverage_tier": "CORE", "source_chunk_ids": ["ch1"]},
            {"title": "主题二", "coverage_tier": "IMPORTANT", "source_chunk_ids": ["ch2"]},
            {"title": "主题三", "coverage_tier": "LOW_FREQUENCY", "source_chunk_ids": ["ch3"]},
        ]
    }
    out = validate_and_normalize_topics(
        raw, coverage_mode="COMPACT", topic_interval=_INTERVAL, **_ARGS
    )
    assert [t["title"] for t in out] == ["主题一"]
    # BALANCED 允许 IMPORTANT、仍禁 LOW_FREQUENCY
    out_balanced = validate_and_normalize_topics(
        raw, coverage_mode="BALANCED", topic_interval=_INTERVAL, **_ARGS
    )
    assert [t["title"] for t in out_balanced] == ["主题一", "主题二"]
    # EXTENSIVE 三级全开
    out_ext = validate_and_normalize_topics(
        raw, coverage_mode="EXTENSIVE", topic_interval=_INTERVAL, **_ARGS
    )
    assert len(out_ext) == 3


def test_filters_unknown_chunk_and_limits() -> None:
    raw = {
        "topics": [
            {"title": "未知页", "coverage_tier": "CORE", "source_chunk_ids": ["ch9"]},
            {"title": "超页数", "coverage_tier": "CORE", "source_chunk_ids": ["ch1", "ch2", "ch3"]},
            {"title": "超字符", "coverage_tier": "CORE", "source_chunk_ids": ["ch2", "ch3"]},
            {"title": "合法", "coverage_tier": "CORE", "source_chunk_ids": ["ch1"]},
        ]
    }
    out = validate_and_normalize_topics(
        raw, coverage_mode="COMPACT", topic_interval=_INTERVAL, **_ARGS
    )
    assert [t["title"] for t in out] == ["合法"]
    assert [t["topic_index"] for t in out] == [1]  # 过滤后重排 1..N


def test_dedupes_normalized_titles() -> None:
    """标题规范化（去空白/标点/大小写）后同义 → 保留首次出现。"""
    raw = {
        "topics": [
            {"title": "Agent 三要素", "coverage_tier": "CORE", "source_chunk_ids": ["ch1"]},
            {"title": "agent三要素！", "coverage_tier": "CORE", "source_chunk_ids": ["ch2"]},
            {"title": "Harness 工程", "coverage_tier": "CORE", "source_chunk_ids": ["ch3"]},
        ]
    }
    out = validate_and_normalize_topics(
        raw, coverage_mode="COMPACT", topic_interval=_INTERVAL, **_ARGS
    )
    assert [t["title"] for t in out] == ["Agent 三要素", "Harness 工程"]
    assert next(t["source_chunk_ids"] for t in out) == ["ch1"]  # 保留首现出处


def test_truncates_to_interval_upper() -> None:
    """主题数 > 区间上限 → 按数组序截断（确定性）。"""
    raw = {
        "topics": [
            {"title": f"主题{i}", "coverage_tier": "CORE", "source_chunk_ids": ["ch1"]}
            for i in range(1, 8)
        ]
    }
    out = validate_and_normalize_topics(
        raw, coverage_mode="COMPACT", topic_interval=(2, 3), **_ARGS
    )
    assert [t["title"] for t in out] == ["主题1", "主题2", "主题3"]


def test_rejects_schema_violations() -> None:
    """结构非法 / 额外键 / 非法层级枚举 → AppError(GENERATION_FAILED)（走预算重试）。"""
    invalid_payloads: list[dict[str, Any]] = [
        {"units": []},  # 错误根键
        {
            "topics": [
                {"title": "x", "coverage_tier": "CORE", "source_chunk_ids": ["ch1"], "hint": 1}
            ]
        },
        {"topics": [{"title": "x", "coverage_tier": "TIER_X", "source_chunk_ids": ["ch1"]}]},
        {"topics": [{"title": "x", "coverage_tier": "CORE", "source_chunk_ids": []}]},
    ]
    for raw in invalid_payloads:
        with pytest.raises(AppError) as ei:
            validate_and_normalize_topics(
                raw, coverage_mode="COMPACT", topic_interval=_INTERVAL, **_ARGS
            )
        assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_invalid_coverage_mode() -> None:
    with pytest.raises(AppError) as ei:
        validate_and_normalize_topics(
            {"topics": []},
            coverage_mode="WHATEVER",
            topic_interval=_INTERVAL,
            **_ARGS,
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_empty_topics_accepted() -> None:
    out = validate_and_normalize_topics(
        {"topics": []}, coverage_mode="EXTENSIVE", topic_interval=_INTERVAL, **_ARGS
    )
    assert out == []


def test_normalize_title_deterministic() -> None:
    assert normalize_title("  Agent，三要素！ ") == normalize_title("agent三要素")
    assert normalize_title("A-B") == "ab"
