"""services.generation.rubric 单元测试（5.9：Rubric 不影响入库）。

T10 起 fake 评分（score_card）退役——Card 评分字段由 SCORING 阶段（T11）LLM 评分回写，
本文件只保留 batch_quality 分布聚合用例（评分用例随 T10 删除，见任务报告）。
"""

from services.generation.rubric import batch_quality


def test_rubric_batch_quality_shape() -> None:
    cards = [
        {"type": "QUESTION", "target_difficulty": "BASIC", "chapter_id": "c1"},
        {"type": "TRUE_FALSE", "target_difficulty": "UNDERSTANDING", "chapter_id": "c1"},
    ]
    q = batch_quality(cards, total_kps=3, duplicated=0)
    assert q["coverage_rate"] == 2 / 3
    assert q["duplicate_rate"] == 0.0
    assert q["difficulty_distribution"]["BASIC"] == 1
    assert q["card_type_distribution"]["QUESTION"] == 1
