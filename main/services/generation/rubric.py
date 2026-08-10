"""Rubric 观测（5.9/8.5：4 维度 0-3 分总分 0-12；Rubric 不影响入库）。

LOCAL-DONE 用 deterministic fake judge（本地规则）；R1 live 用 LLM-as-judge
（scoring-prompt.md 资产）——fake 不代替生产（红线）。
"""

from typing import Any


def score_card(card: dict[str, Any]) -> dict[str, Any]:
    """确定性评分：基于字段完整度/长度/一致性。返回 {4 维度分, rubric_total_score}。"""
    q_len = len(str(card.get("question") or card.get("statement") or ""))
    a_len = len(str(card.get("answer") or card.get("explanation") or ""))
    evidence = 3 if q_len >= 30 else (2 if q_len >= 10 else 1)
    correctness = 3 if a_len >= 30 else (2 if a_len >= 10 else (1 if a_len > 0 else 0))
    difficulty = (
        2
        if card.get("target_difficulty") == "APPLICATION"
        else (1 if card.get("target_difficulty") == "UNDERSTANDING" else 0)
    )
    learning = 3 if card.get("explanation") else 2
    total = evidence + correctness + difficulty + learning
    return {
        "evidence_score": evidence,
        "correctness_score": correctness,
        "difficulty_score": difficulty,
        "learning_value_score": learning,
        "rubric_total_score": total,
    }


def batch_quality(
    cards: list[dict[str, Any]], *, total_kps: int, duplicated: int
) -> dict[str, Any]:
    """批次质量统计（5.10：覆盖率/重复率/难度/章节/类型分布/难度偏差——仅观测）。"""
    n = len(cards)
    return {
        "coverage_rate": n / total_kps if total_kps else 0.0,
        "duplicate_rate": duplicated / n if n else 0.0,
        "difficulty_distribution": _dist(cards, "target_difficulty"),
        "chapter_distribution": _dist(cards, "chapter_id"),
        "card_type_distribution": _dist(cards, "type"),
        "difficulty_deviation": 0.0,  # V5A 简化（观测字段结构完整）
    }


def _dist(cards: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cards:
        v = str(c.get(key) or "unknown")
        out[v] = out.get(v, 0) + 1
    return out
