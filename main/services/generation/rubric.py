"""Rubric 观测（spec §8：四维 0-3 分总分 0-12；Rubric 不影响入库）。

T10 起 fake 评分退役（brief Step 4）：Card 5 个评分字段由 SCORING 阶段（T11）LLM
评分回写，生成期一律留 NULL；本模块只保留 batch_quality 分布聚合（Batch 质量列）。
"""

from typing import Any


def batch_quality(
    cards: list[dict[str, Any]], *, total_kps: int, duplicated: int
) -> dict[str, Any]:
    """批次质量统计（5.10：覆盖率/重复率/难度/章节/类型分布/难度偏差——仅观测）。

    批=单元语义（spec §7）：coverage_rate = 该单元是否产出合法卡（0/1，不再恒定 1.0）；
    分布字段为单值（difficulty 按单元锚定 target_difficulty 归因）。
    """
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
