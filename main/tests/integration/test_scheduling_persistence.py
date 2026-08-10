"""排程结果持久化映射集成测试：py-fsrs Card ↔ review_states 表字段（database-design 2.10）。

校准说明（py-fsrs 6.3.2）：Card 无 reps/lapses 属性（ORM 对应列由 Task 2 review
service 计数）；State 为 IntEnum，.name 与 ORM state 字符串列（NEW/LEARNING/REVIEW/
RELEARNING，见 infra/db/models.py ReviewState）对应。
"""

from datetime import datetime

from fsrs import Card, Rating

from services.scheduling.scheduler import create_scheduler, review_card


def test_scheduling_card_fields_map_to_review_state_columns() -> None:
    """py-fsrs Card 输出字段与 review_states 列类型兼容。"""
    scheduler = create_scheduler()
    new_card, _ = review_card(scheduler, Card(), Rating.Good)
    assert isinstance(new_card.stability, float)
    assert isinstance(new_card.difficulty, float)
    assert isinstance(new_card.due, datetime)
    assert new_card.due.tzinfo is not None
    assert new_card.state.name in ("Learning", "Review", "Relearning")
