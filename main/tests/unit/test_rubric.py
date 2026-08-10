"""services.generation.rubric 单元测试（5.9：4 维度 0-3 分总分 0-12，Rubric 不影响入库）。"""

from services.generation.rubric import batch_quality, score_card


def test_rubric_score_deterministic_and_in_range() -> None:
    card = {"type": "QUESTION", "question": "q" * 20, "answer": "a" * 20}
    s1 = score_card(card)
    s2 = score_card(card)
    assert s1 == s2  # deterministic
    for k in ("evidence_score", "correctness_score", "difficulty_score", "learning_value_score"):
        assert 0 <= s1[k] <= 3
    assert 0 <= s1["rubric_total_score"] <= 12


def test_rubric_score_different_for_different_cards() -> None:
    rich = {"type": "QUESTION", "question": "q" * 50, "answer": "a" * 50, "explanation": "e" * 30}
    poor = {"type": "QUESTION", "question": "q", "answer": ""}
    assert score_card(rich)["rubric_total_score"] >= score_card(poor)["rubric_total_score"]


def test_rubric_batch_quality_shape() -> None:
    cards = [
        {"type": "QUESTION", "target_difficulty": "BASIC", "chapter_id": "c1"},
        {"type": "TRUE_FALSE", "target_difficulty": "APPLICATION", "chapter_id": "c1"},
    ]
    q = batch_quality(cards, total_kps=3, duplicated=0)
    assert q["coverage_rate"] == 2 / 3
    assert q["duplicate_rate"] == 0.0
    assert q["difficulty_distribution"]["BASIC"] == 1
    assert q["card_type_distribution"]["QUESTION"] == 1
