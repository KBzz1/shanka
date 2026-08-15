"""当前 LLM 资产结构守卫：入口、输入信封、fail-closed 语义与 JSON Schema。

资产版本随 manifest 演进至 v4/v3（Task 7）；版本对齐与标签/语义守卫见
test_prompt_assets_v4.py，本文件只保留对当前资产通用的结构断言。"""

import json
from typing import Any

import jsonschema
import pytest

from infra.llm.prompts import asset_versions, load_schema_asset
from tests.contract.support import MANIFEST_PATH, load_manifest

ASSET_ROOT = MANIFEST_PATH.parent


def _asset(section: str, name: str) -> str:
    manifest = load_manifest()
    path = ASSET_ROOT / str(manifest[section][name]["path"])
    return path.read_text(encoding="utf-8")


def _schema(name: str) -> dict[str, Any]:
    data = json.loads(_asset("schemas", name))
    assert isinstance(data, dict)
    jsonschema.Draft202012Validator.check_schema(data)
    return data


def test_prompt_assets_use_structured_runtime_envelopes() -> None:
    expected = {
        "planner": "<PLANNER_INPUT>",
        "generator": "<GENERATOR_INPUT>",
        "rewrite": "<REWRITE_INPUT>",
        "scoring": "<SCORING_INPUT>",
    }
    for name, marker in expected.items():
        text = _asset("prompts", name)
        assert marker in text
        assert "不可信" in text or "只是数据" in text
        assert "JSON" in text


def test_generator_v3_fails_closed_without_source_support() -> None:
    text = _asset("prompts", "generator")
    assert '{"cards":[]}' in text
    assert "不得用训练记忆" in text
    assert "软长度目标" in text
    assert "模板化元话语" in text
    assert "示例 3" in text


def test_generator_output_v2_schema_wraps_zero_or_one_minimal_card() -> None:
    schema = _schema("generator_output")
    cards = schema["properties"]["cards"]
    assert cards["minItems"] == 0
    assert cards["maxItems"] == 1
    jsonschema.validate({"cards": []}, schema)
    jsonschema.validate(
        {"cards": [{"type": "QUESTION", "question": "什么是 A？", "answer": "A 是……"}]},
        schema,
    )
    jsonschema.validate(
        {
            "cards": [
                {
                    "type": "TRUE_FALSE",
                    "statement": "A 成立。",
                    "answer_boolean": True,
                    "explanation": "来源明确给出 A。",
                }
            ]
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "cards": [
                    {
                        "type": "QUESTION",
                        "front": "冗余投影字段",
                        "question": "什么是 A？",
                        "answer": "A 是……",
                    }
                ]
            },
            schema,
        )


def test_planner_output_schema_contract() -> None:
    schema = _schema("planner_output")
    assert schema["additionalProperties"] is False
    unit = schema["properties"]["units"]["items"]
    assert unit["additionalProperties"] is False
    assert set(unit["required"]) == {
        "source_chunk_ids",
        "learning_objective",
        "target_difficulty",
        "card_type",
        "coverage_tier",
    }
    assert "priority" not in unit["properties"]
    jsonschema.validate({"units": []}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"units": [], "unexpected": True}, schema)


def test_scoring_output_v2_schema_contract() -> None:
    schema = _schema("scoring_output")
    assert schema["additionalProperties"] is False
    score = schema["properties"]["scores"]["items"]
    assert score["additionalProperties"] is False
    assert score["properties"]["evidence_score"]["maximum"] == 3
    assert "rubric_total_score" not in score["properties"]
    valid = {
        "scores": [
            {
                "generation_item_id": "item-1",
                "evidence_score": 3,
                "correctness_score": 3,
                "difficulty_score": 3,
                "learning_value_score": 3,
            }
        ]
    }
    jsonschema.validate(valid, schema)
    invalid = json.loads(json.dumps(valid))
    invalid["scores"][0]["evidence_score"] = 4
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, schema)


def test_manifest_has_new_entries() -> None:
    manifest = load_manifest()
    assert "scoring" in manifest["prompts"]
    assert manifest["prompts"]["scoring"]["version"] == "v3"
    assert "planner_output" in manifest["schemas"]
    assert "scoring_output" in manifest["schemas"]


def test_versions_extended() -> None:
    v = asset_versions()
    assert v["generator_prompt_version"] == "v4"
    assert v["planner_prompt_version"] == "v4"
    assert v["rewrite_prompt_version"] == "v4"
    assert v["scoring_prompt_version"] == "v3"
    assert v["card_schema_version"] == "v1"
    assert v["planner_output_schema_version"] == "v3"
    assert v["scoring_output_schema_version"] == "v3"
    assert v["rubric_version"] == "v3"


def test_versions_keep_backward_compat_keys() -> None:
    manifest = load_manifest()
    v = asset_versions()
    # 兼容键语义：prompt_version=generator prompt；schema_version=generator-output schema
    assert v["prompt_version"] == manifest["prompts"]["generator"]["version"]
    assert v["schema_version"] == manifest["schemas"]["generator_output"]["version"]


def test_load_schema_asset_parses_json() -> None:
    planner = load_schema_asset("planner_output")
    assert planner["required"] == ["units"]
    scoring = load_schema_asset("scoring_output")
    assert scoring["required"] == ["scores"]


def test_all_manifest_asset_paths_stay_inside_agent_evolution() -> None:
    root = ASSET_ROOT.resolve()
    manifest = load_manifest()
    for section in ("prompts", "schemas", "rubrics"):
        for entry in manifest[section].values():
            path = (ASSET_ROOT / entry["path"]).resolve()
            assert path.is_relative_to(root)
            assert path.is_file()
