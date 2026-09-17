"""当前 LLM 资产结构守卫：入口、输入信封、fail-closed 语义与 JSON Schema。

资产版本随 manifest 演进至 v4/v3（Task 7）；版本对齐与标签/语义守卫见
test_prompt_assets_v4.py，本文件只保留对当前资产通用的结构断言。"""

import json
from typing import Any

import jsonschema
import pytest

from infra.llm.prompts import asset_versions, load_schema_asset
from tests.contract.support import MANIFEST_PATH, load_manifest

COVERAGE_TIERS = ("CORE", "IMPORTANT", "LOW_FREQUENCY")

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
        "planner_coarse": "<PLANNER_COARSE_INPUT>",
        "chapter_planner": "<CHAPTER_PLANNER_INPUT>",
        "generator": "<GENERATION_SPEC>",
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
    # V2.5.2 两阶段：units 增 topic_index（锚定分配主题），coverage_tier 由服务端注入
    assert set(unit["required"]) == {
        "topic_index",
        "source_chunk_ids",
        "learning_objective",
        "target_difficulty",
        "card_type",
    }
    assert "priority" not in unit["properties"]
    assert "coverage_tier" not in unit["properties"]
    assert unit["properties"]["topic_index"]["minimum"] == 1
    jsonschema.validate({"units": []}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"units": [], "unexpected": True}, schema)


def test_planner_coarse_output_schema_contract() -> None:
    schema = _schema("planner_coarse_output")
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["topics"]
    topic = schema["properties"]["topics"]["items"]
    assert topic["additionalProperties"] is False
    assert set(topic["required"]) == {"title", "coverage_tier", "source_chunk_ids"}
    assert topic["properties"]["coverage_tier"]["enum"] == list(COVERAGE_TIERS)
    assert "topic_index" not in topic["properties"]  # 服务端分配，模型不输出
    jsonschema.validate({"topics": []}, schema)


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
    # V25-D-36 无目录 PDF 章节边界规划（v10 分层原理，2026-09-17）
    assert manifest["prompts"]["chapter_planner"]["version"] == "v10"
    assert manifest["schemas"]["chapter_planner_output"]["version"] == "v9"


def test_chapter_planner_output_schema_contract() -> None:
    """V25-D-36 章节边界规划输出：只含 title/start_page，结构层禁止额外字段。"""
    schema = _schema("chapter_planner_output")
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["chapters"]
    chapter = schema["properties"]["chapters"]["items"]
    assert chapter["additionalProperties"] is False
    assert set(chapter["required"]) == {"title", "start_page"}
    assert chapter["properties"]["start_page"]["minimum"] == 1
    jsonschema.validate({"chapters": []}, schema)
    jsonschema.validate({"chapters": [{"title": "第 1 章 绪论", "start_page": 3}]}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"chapters": [{"title": "缺页码"}]}, schema)


def test_chapter_planner_prompt_reports_only_in_segment_boundaries() -> None:
    """段边界契约（V25-D-36）：只报本段内开始的边界、跨段延续不报、宁缺毋滥、页码接地。"""
    text = _asset("prompts", "chapter_planner")
    assert "在本段内开始" in text
    assert "上一章的延续" in text
    assert "宁缺毋滥" in text
    assert "只能引用" in text  # 页码接地（防幻觉页码）


def test_chapter_planner_prompt_v10_layered_principle() -> None:
    """v10 分层原理契约（两书量化评测驱动）：枚举改原理、中文序号同权、条目行排除。"""
    text = _asset("prompts", "chapter_planner")
    assert "编号跨度" in text and "重复频次" in text  # 分层维度
    assert "跨度最大、重复频次最低" in text  # 章节体系判定
    assert "一/二/三" in text  # 中文序号同权
    assert "条目" in text  # 高频编号行排除
    assert "必须报告" in text  # 显式节头不适用宁缺毋滥


def test_versions_extended() -> None:
    v = asset_versions()
    assert v["generator_prompt_version"] == "v7"
    assert v["planner_prompt_version"] == "v8"
    assert v["planner_coarse_prompt_version"] == "v8"
    assert v["chapter_planner_prompt_version"] == "v10"
    assert v["rewrite_prompt_version"] == "v4"
    assert v["scoring_prompt_version"] == "v3"
    assert v["card_schema_version"] == "v1"
    assert v["planner_output_schema_version"] == "v7"
    assert v["planner_coarse_output_schema_version"] == "v7"
    assert v["chapter_planner_output_schema_version"] == "v9"
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
