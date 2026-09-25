"""qa_planner_validator 单测（V25-D-43）：Schema 双形态、来源接地、上限与去重键。"""

from typing import Any

import pytest

from app.errors import AppError
from services.generation.qa_planner_validator import (
    normalize_question,
    validate_and_normalize_pairs,
)

_PAGE_CHARS = {"c1": 100, "c2": 200, "c3": 50}


def _validate(raw: dict[str, Any], **overrides: Any) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "allowed_page_ids": set(_PAGE_CHARS),
        "max_chunks_per_pair": 8,
        "max_chars_per_pair": 10_000,
        "page_chars": _PAGE_CHARS,
    }
    kwargs.update(overrides)
    return validate_and_normalize_pairs(raw, **kwargs)


def test_question_pair_passes_and_normalizes_chunk_order() -> None:
    pairs = _validate(
        {
            "qa_pairs": [
                {
                    "card_type": "QUESTION",
                    "question": "什么是上下文窗口？",
                    "source_chunk_ids": ["c2", "c1"],
                }
            ]
        }
    )
    assert pairs[0]["source_chunk_ids"] == ["c1", "c2"]  # 页序重排
    assert pairs[0]["question"] == "什么是上下文窗口？"


def test_true_false_pair_shape() -> None:
    pairs = _validate(
        {
            "qa_pairs": [
                {
                    "card_type": "TRUE_FALSE",
                    "statement": "上下文窗口越长，可读取文本越多。",
                    "source_chunk_ids": ["c1"],
                }
            ]
        }
    )
    assert pairs[0]["statement"] == "上下文窗口越长，可读取文本越多。"


def test_stale_answer_keys_stripped() -> None:
    """输出经济（v1 定稿形态）：模型仍回抄答案类键时防御性剥除，不烧重试预算。"""
    pairs = _validate(
        {
            "qa_pairs": [
                {
                    "card_type": "QUESTION",
                    "question": "什么是上下文窗口？",
                    "answer": "模型一次推理能读取的最大文本长度。",
                    "source_chunk_ids": ["c1"],
                },
                {
                    "card_type": "TRUE_FALSE",
                    "statement": "上下文窗口越长，可读取文本越多。",
                    "answer_boolean": True,
                    "explanation": "资料解析。",
                    "source_chunk_ids": ["c2"],
                },
            ]
        }
    )
    assert set(pairs[0]) == {"card_type", "question", "source_chunk_ids"}
    assert set(pairs[1]) == {"card_type", "statement", "source_chunk_ids"}


def test_extra_key_rejected() -> None:
    with pytest.raises(AppError):
        _validate(
            {
                "qa_pairs": [
                    {
                        "card_type": "QUESTION",
                        "question": "q",
                        "answer": "a",
                        "source_chunk_ids": ["c1"],
                        "priority": 1,
                    }
                ]
            }
        )


def test_chunk_grounding_enforced() -> None:
    with pytest.raises(AppError):
        _validate(
            {
                "qa_pairs": [
                    {
                        "card_type": "QUESTION",
                        "question": "q",
                        "answer": "a",
                        "source_chunk_ids": ["unknown"],
                    }
                ]
            }
        )


def test_chunk_count_cap_enforced() -> None:
    with pytest.raises(AppError):
        _validate(
            {
                "qa_pairs": [
                    {
                        "card_type": "QUESTION",
                        "question": "q",
                        "answer": "a",
                        "source_chunk_ids": ["c1", "c2", "c3"],
                    }
                ]
            },
            max_chunks_per_pair=2,
        )


def test_normalize_question_strips_numbering_and_whitespace() -> None:
    assert normalize_question("12. 什么是上下文窗口？") == normalize_question("什么是上下文窗口？")
    assert normalize_question("Q3: 什么是上下文窗口?") == normalize_question("什么是上下文窗口?")
    assert normalize_question("（2）什么是 上下文窗口？") == normalize_question(
        "什么是上下文窗口？"
    )
    assert normalize_question("第 3 题、什么是上下文窗口？") == normalize_question(
        "什么是上下文窗口？"
    )
