"""services.generation.fake 确定性单元测试。"""

from services.generation.fake import generate_card


def test_fake_card_deterministic() -> None:
    c1 = generate_card("FSRS", "第一章", "BASIC", None)
    c2 = generate_card("FSRS", "第一章", "BASIC", None)
    assert c1 == c2  # 同输入同输出
    assert c1["front"] and c1["back"]


def test_fake_card_differs_by_input() -> None:
    a = generate_card("主题A", "第一章", "BASIC", None)
    b = generate_card("主题B", "第一章", "BASIC", None)
    assert a["front"] != b["front"]


def test_fake_card_type_by_difficulty() -> None:
    basic = generate_card("t", "c", "BASIC", None)
    app = generate_card("t", "c", "APPLICATION", None)
    assert basic["card_type"] == "QUESTION"
    assert app["card_type"] == "TRUE_FALSE"


def test_fake_card_ids_stable_and_unique() -> None:
    a = generate_card("主题X", "c", "BASIC", None)
    b = generate_card("主题X", "c", "APPLICATION", None)
    assert a["card_id"] != b["card_id"]
    assert a["generation_item_id"] != b["generation_item_id"]
