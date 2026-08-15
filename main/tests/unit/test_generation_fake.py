"""services.generation.fake 确定性单元测试。"""

from services.generation.fake import generate_card


def test_fake_card_deterministic() -> None:
    c1 = generate_card("FSRS", "第一章", "BASIC", None, "task-1")
    c2 = generate_card("FSRS", "第一章", "BASIC", None, "task-1")
    assert c1 == c2  # 同输入同输出
    assert c1["front"] and c1["back"]


def test_fake_card_differs_by_input() -> None:
    a = generate_card("主题A", "第一章", "BASIC", None, "task-1")
    b = generate_card("主题B", "第一章", "BASIC", None, "task-1")
    assert a["front"] != b["front"]


def test_fake_card_type_by_difficulty() -> None:
    basic = generate_card("t", "c", "BASIC", None, "task-1")
    deep = generate_card("t", "c", "DEEP_QUESTION", None, "task-1")  # V2.5 改名
    assert basic["card_type"] == "QUESTION"
    # V2.5：DEEP_QUESTION 只允许 QUESTION 卡型（契约 3.6 组合规则）
    assert deep["card_type"] == "QUESTION"
    assert deep["target_difficulty"] == "DEEP_QUESTION"


def test_fake_card_ids_stable_and_unique() -> None:
    a = generate_card("主题X", "c", "BASIC", None, "task-1")
    b = generate_card("主题X", "c", "DEEP_QUESTION", None, "task-1")  # V2.5 改名
    assert a["card_id"] != b["card_id"]
    assert a["generation_item_id"] != b["generation_item_id"]


def test_fake_card_task_dimension_in_seed() -> None:
    """F-1 防回退：task_id 纳入 seed——同内容不同任务不共享 generation_item_id。"""
    a = generate_card("t", "c", "BASIC", None, "task-1")
    b = generate_card("t", "c", "BASIC", None, "task-2")
    assert a["generation_item_id"] != b["generation_item_id"]
    assert a["card_id"] != b["card_id"]
