"""services.generation.schema_validator 单元测试（5.8 Schema 唯一入库门槛）。

卡片形状按 card.schema.json 实际结构校准（Draft 2020-12）：
- 顶层 required = [type, front, back]；
- allOf 条件分支：QUESTION 要求 question/answer，TRUE_FALSE 要求 statement/answer_boolean/explanation。
"""

from services.generation.schema_validator import load_card_schema, validate_card


def test_schema_validator_question_card_valid() -> None:
    schema = load_card_schema()
    card = {
        "type": "QUESTION",
        "front": "什么是 FSRS？",
        "back": "间隔重复算法",
        "question": "什么是 FSRS？",
        "answer": "间隔重复算法",
    }
    assert validate_card(card, schema) == []


def test_schema_validator_true_false_card_valid() -> None:
    schema = load_card_schema()
    card = {
        "type": "TRUE_FALSE",
        "front": "FSRS 是间隔重复算法",
        "back": "是",
        "statement": "FSRS 是间隔重复算法",
        "answer_boolean": True,
        "explanation": "是",
    }
    assert validate_card(card, schema) == []


def test_schema_validator_missing_fields_invalid() -> None:
    schema = load_card_schema()
    violations = validate_card({"type": "QUESTION", "question": "q"}, schema)
    assert any("answer" in v for v in violations)


def test_schema_validator_wrong_types_invalid() -> None:
    schema = load_card_schema()
    violations = validate_card({"type": "QUESTION", "question": "q", "answer": 123}, schema)
    assert violations  # answer 类型非法
