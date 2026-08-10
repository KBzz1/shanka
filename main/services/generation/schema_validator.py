"""Schema 校验器（5.8/AC-04：Schema 是唯一入库门槛，Rubric 不影响入库）。

校验源：agent_evolution/schemas/v1/card.schema.json（manifest 唯一入口——infra/llm/prompts 的
load_asset("schemas", "card") 复用；资产演进 R-03 不原地改 v1）。
"""

import json
from typing import Any

import jsonschema

from infra.llm.prompts import load_asset


def load_card_schema() -> dict[str, Any]:
    data = json.loads(load_asset("schemas", "card"))
    assert isinstance(data, dict)
    return data


def validate_card(card: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """校验卡片。返回违约列表（空 = 合法）。"""
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{err.json_path or err.path}: {err.message}" for err in validator.iter_errors(card)]
