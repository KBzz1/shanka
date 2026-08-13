"""planner_validator 测试（spec §5.2/§5.6/§3.5；Task 9）。

- Schema（planner-output v2）负责根包装/结构/枚举/必填/禁额外键；
- 代码层校验来源子集、页数/字符数上限；按难度子配额确定性截断（priority 升序、
  并列按原顺序）；page_chars 按页序（插入序 = load_pages 页序）规范化来源顺序；
- 输入容忍服务端 priority 提示键（模型契约不含；缺失时按数组序）。
"""

import pytest

from app.errors import AppError, ErrorCode
from services.generation.planner_validator import normalize_units, validate_and_truncate


def test_truncate_by_quota_and_normalize() -> None:
    """按难度配额截断（priority 升序）+ source_chunk_ids 页序规范化 + priority 重排 1..N。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch3", "ch1"],
                "learning_objective": "b1",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 1,
            },
            {
                "source_chunk_ids": ["ch1", "ch3"],
                "learning_objective": "b2",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 2,
            },
            {
                "source_chunk_ids": ["ch3"],
                "learning_objective": "u1",
                "target_difficulty": "UNDERSTANDING",
                "card_type": "TRUE_FALSE",
                "priority": 1,
            },
        ]
    }
    out = validate_and_truncate(
        raw,
        allowed_page_ids={"ch1", "ch3"},
        quota={"BASIC": 1, "UNDERSTANDING": 1, "APPLICATION": 0},
        max_pages_per_unit=2,
        max_chars_per_unit=9999,
        page_chars={"ch1": 10, "ch3": 20},
    )
    assert [u["learning_objective"] for u in out] == ["b1", "u1"]
    assert out[0]["source_chunk_ids"] == ["ch1", "ch3"]  # page_number（页序）排序规范化
    assert [u["priority"] for u in out] == [1, 2]  # priority 重排 1..N


def test_rejects_outside_pages() -> None:
    """来源引用超出本次调用页集合 → AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch9"],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 1,
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_extra_field() -> None:
    """Schema 禁额外键：非 priority 的未知键 → AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "hint": "注入尝试",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_missing_required_field() -> None:
    """缺 learning_objective（Schema required）→ AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_empty_source_chunk_ids() -> None:
    """source_chunk_ids 至少 1 项（Schema minItems）→ AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": [],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_duplicate_source_chunk_ids() -> None:
    """source_chunk_ids 不得重复（Schema uniqueItems）→ AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1", "ch1"],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_anchor_enum_violation() -> None:
    """锚定值越界（target_difficulty 非枚举）→ AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "learning_objective": "x",
                "target_difficulty": "EXPERT",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_too_many_pages() -> None:
    """单元来源页数 > max_pages_per_unit → AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1", "ch2", "ch3"],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1", "ch2", "ch3"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=9999,
            page_chars={"ch1": 1, "ch2": 1, "ch3": 1},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_rejects_too_many_chars() -> None:
    """单元来源字符和 > max_chars_per_unit → AppError(GENERATION_FAILED)。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1", "ch2"],
                "learning_objective": "x",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            }
        ]
    }
    with pytest.raises(AppError) as ei:
        validate_and_truncate(
            raw,
            allowed_page_ids={"ch1", "ch2"},
            quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
            max_pages_per_unit=2,
            max_chars_per_unit=5,
            page_chars={"ch1": 3, "ch2": 3},
        )
    assert ei.value.code is ErrorCode.GENERATION_FAILED


def test_priority_absent_uses_array_order() -> None:
    """模型输出无 priority（契约）：截断按数组顺序（priority 升序退化为原顺序）。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "learning_objective": "b1",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            },
            {
                "source_chunk_ids": ["ch2"],
                "learning_objective": "b2",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            },
        ]
    }
    out = validate_and_truncate(
        raw,
        allowed_page_ids={"ch1", "ch2"},
        quota={"BASIC": 1, "UNDERSTANDING": 0, "APPLICATION": 0},
        max_pages_per_unit=2,
        max_chars_per_unit=9999,
        page_chars={"ch1": 10, "ch2": 20},
    )
    assert [u["learning_objective"] for u in out] == ["b1"]  # 保留数组序首位


def test_truncation_tie_keeps_original_order() -> None:
    """同难度同 priority 并列 → 按原数组顺序保留（确定性）。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "learning_objective": "first",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 1,
            },
            {
                "source_chunk_ids": ["ch2"],
                "learning_objective": "second",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 1,
            },
        ]
    }
    out = validate_and_truncate(
        raw,
        allowed_page_ids={"ch1", "ch2"},
        quota={"BASIC": 1, "UNDERSTANDING": 0, "APPLICATION": 0},
        max_pages_per_unit=2,
        max_chars_per_unit=9999,
        page_chars={"ch1": 10, "ch2": 20},
    )
    assert [u["learning_objective"] for u in out] == ["first"]


def test_zero_quota_removes_difficulty() -> None:
    """某难度配额为 0 → 该难度单元全部截断（0 表示禁止输出）。"""
    raw = {
        "units": [
            {
                "source_chunk_ids": ["ch1"],
                "learning_objective": "a1",
                "target_difficulty": "APPLICATION",
                "card_type": "QUESTION",
            }
        ]
    }
    out = validate_and_truncate(
        raw,
        allowed_page_ids={"ch1"},
        quota={"BASIC": 1, "UNDERSTANDING": 1, "APPLICATION": 0},
        max_pages_per_unit=2,
        max_chars_per_unit=9999,
        page_chars={"ch1": 10},
    )
    assert out == []


def test_empty_units_accepted() -> None:
    """合法空结果 {"units": []}（安全弃权）→ 返回空列表。"""
    out = validate_and_truncate(
        {"units": []},
        allowed_page_ids={"ch1"},
        quota={"BASIC": 1, "UNDERSTANDING": 1, "APPLICATION": 1},
        max_pages_per_unit=2,
        max_chars_per_unit=9999,
        page_chars={"ch1": 10},
    )
    assert out == []


def test_normalize_units_dedupes_and_renumbers() -> None:
    """normalize_units：source_chunk_ids 去重（保序）+ priority 重排 1..N。"""
    out = normalize_units(
        [
            {
                "source_chunk_ids": ["ch1", "ch1", "ch3"],
                "learning_objective": "a",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
            },
            {
                "source_chunk_ids": ["ch3"],
                "learning_objective": "b",
                "target_difficulty": "UNDERSTANDING",
                "card_type": "TRUE_FALSE",
            },
        ]
    )
    assert out[0]["source_chunk_ids"] == ["ch1", "ch3"]
    assert [u["priority"] for u in out] == [1, 2]
