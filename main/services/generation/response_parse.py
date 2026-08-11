"""services.generation.response_parse：LLM 响应 JSON → 内部卡 dict（重写单卡与批次生成共用，DRY）。

- parse_cards_json：响应 content JSON → 卡片 dict 列表（非 JSON / 无 cards 列表 → []）；
- to_internal_card：响应卡片 → 内部卡 dict（派生 front/back；Schema 是唯一入库门槛）。

V6 从 batches.py 提取（原 _parse_cards/_to_internal_card，仅改名，行为零变化）：重写单卡与批次生成共用。
"""

import json
from typing import Any


def parse_cards_json(content: str) -> list[dict[str, Any]]:
    """响应 content JSON → 卡片列表。非 JSON / 无 cards 列表 → []（0 合法卡 → FAILED/重试）。"""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    cards = data.get("cards")
    if not isinstance(cards, list):
        return []
    return [c for c in cards if isinstance(c, dict)]


def to_internal_card(card: dict[str, Any]) -> dict[str, Any]:
    """响应卡片 → 内部卡 dict（T1 carry-forward：必须产出 front/back，否则 Schema 违约）。

    - QUESTION：front/back 缺失时从 question/answer 派生（生成输出允许不带 front/back）；
    - TRUE_FALSE：front/back 缺失时从 statement/explanation 派生；
    - 派生后仍缺失 → 对应键为 None → Schema 校验违约，不入库（唯一门槛）。
    """
    ctype = card.get("type")
    if ctype == "QUESTION":
        return {
            "type": "QUESTION",
            "front": card.get("front") or card.get("question"),
            "back": card.get("back") or card.get("answer"),
            "question": card.get("question"),
            "answer": card.get("answer"),
        }
    if ctype == "TRUE_FALSE":
        return {
            "type": "TRUE_FALSE",
            "front": card.get("front") or card.get("statement"),
            "back": card.get("back") or card.get("explanation"),
            "statement": card.get("statement"),
            "answer_boolean": card.get("answer_boolean"),
            "explanation": card.get("explanation"),
        }
    return dict(card)
