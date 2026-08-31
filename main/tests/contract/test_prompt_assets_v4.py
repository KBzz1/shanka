"""v4/v3 LLM 资产守卫（Task 7 NV-04）：manifest/CHANGELOG 版本对齐、schema JSON 合法性、
allowed labels（CORE/IMPORTANT/LOW_FREQUENCY）、source chunk ID 接地，以及禁止
count/cost/pause 语义进资产。

历史版本目录（prompts/v3、schemas/v2、rubrics/v2 及更早）禁止原地修改；本文件只守卫
manifest 当前入口指向的 v4/v3 资产。
"""

import json
from typing import Any

import jsonschema
import pytest

from tests.contract.support import MANIFEST_PATH, load_manifest

ASSET_ROOT = MANIFEST_PATH.parent

# 难度值域（domain/enums.py Difficulty）与覆盖层级标签（任务 7 接口：语义单元标注覆盖层级）
DIFFICULTIES = ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")
COVERAGE_TIERS = ("CORE", "IMPORTANT", "LOW_FREQUENCY")
COVERAGE_MODES = ("COMPACT", "BALANCED", "EXTENSIVE")

# 禁止进资产的 count/cost/pause 语义词（覆盖深度不是数量承诺，成本/暂停属代码职责）
_FORBIDDEN_TOKENS = (
    "凑够",
    "费用",
    "价格",
    "成本",
    "暂停",
    "预计卡数",
    "总卡数",
    "token",
    "Token",
    "pause",
    "Pause",
    "PAUSE",
)


def _asset(section: str, name: str) -> str:
    manifest = load_manifest()
    path = ASSET_ROOT / str(manifest[section][name]["path"])
    return path.read_text(encoding="utf-8")


def _schema(name: str) -> dict[str, Any]:
    data = json.loads(_asset("schemas", name))
    assert isinstance(data, dict)
    jsonschema.Draft202012Validator.check_schema(data)
    return data


def _manifest_versions() -> set[str]:
    """当前 manifest 全部资产版本（card v1 为未变基线，不要求 CHANGELOG 记录）。"""
    manifest = load_manifest()
    versions = {
        entry["version"]
        for section, entries in manifest.items()
        for name, entry in entries.items()
        if not (section == "schemas" and name == "card")
    }
    assert all(isinstance(v, str) for v in versions)
    return versions


def test_manifest_pins_v4_v3_versions_and_paths() -> None:
    """manifest 升版：prompts v4、schemas（除 card v1）v3、rubrics v3，path 指向新目录。"""
    manifest = load_manifest()
    assert manifest["prompts"]["planner"]["version"] == "v5"
    assert manifest["prompts"]["generator"]["version"] == "v5"
    assert manifest["prompts"]["rewrite"]["version"] == "v4"
    assert manifest["prompts"]["scoring"]["version"] == "v3"
    assert manifest["schemas"]["card"]["version"] == "v1"  # 持久化 Card Schema 保持 v1
    # 密度制 V25-D-25/26/27：planner v5 / generator v5 / planner_output v4；其余保持
    assert manifest["schemas"]["planner_output"]["version"] == "v4"
    assert manifest["schemas"]["generator_output"]["version"] == "v3"
    assert manifest["schemas"]["scoring_output"]["version"] == "v3"
    assert manifest["rubrics"]["main"]["version"] == "v3"
    for name in ("planner", "generator"):
        assert str(manifest["prompts"][name]["path"]).startswith("prompts/v5/")
    assert str(manifest["prompts"]["rewrite"]["path"]).startswith("prompts/v4/")
    assert str(manifest["prompts"]["scoring"]["path"]).startswith("rubrics/v3/")
    for name, entry in manifest["schemas"].items():
        if name == "card":
            expected = "schemas/v1/"
        elif name == "planner_output":
            expected = "schemas/v4/"
        else:
            expected = "schemas/v3/"
        assert str(entry["path"]).startswith(expected)
    assert str(manifest["rubrics"]["main"]["path"]).startswith("rubrics/v3/")


def test_changelog_latest_section_documents_current_versions() -> None:
    """CHANGELOG 最新小节必须覆盖 manifest 当前全部资产版本（manifest+CHANGELOG 同 commit）。"""
    changelog = (ASSET_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = [s for s in changelog.split("\n## ")[1:]]
    assert sections, "CHANGELOG 必须至少有一个版本小节"
    latest = sections[-1]  # 演进日志按时间追加，最新小节在末尾
    for version in sorted(_manifest_versions()):
        assert version in latest, f"CHANGELOG 最新小节未记录当前资产版本 {version}"


def test_v4_v3_schemas_are_valid_json_schema() -> None:
    for name in ("planner_output", "generator_output", "scoring_output"):
        jsonschema.Draft202012Validator.check_schema(_schema(name))


def test_planner_schema_v3_difficulty_enum_and_coverage_tier_labels() -> None:
    """planner-output v3：难度枚举只允许 BASIC/UNDERSTANDING/DEEP_QUESTION；
    每单元必须携带 coverage_tier 标签（CORE/IMPORTANT/LOW_FREQUENCY）。"""
    schema = _schema("planner_output")
    unit = schema["properties"]["units"]["items"]
    assert unit["additionalProperties"] is False
    difficulty = unit["properties"]["target_difficulty"]["enum"]
    assert difficulty == list(DIFFICULTIES)
    assert "APPLICATION" not in difficulty
    assert unit["properties"]["coverage_tier"]["enum"] == list(COVERAGE_TIERS)
    assert "coverage_tier" in unit["required"]
    jsonschema.validate(
        {
            "units": [
                {
                    "source_chunk_ids": ["ch1"],
                    "learning_objective": "说出 A 的判定条件",
                    "target_difficulty": "DEEP_QUESTION",
                    "card_type": "QUESTION",
                    "coverage_tier": "CORE",
                }
            ]
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "units": [
                    {
                        "source_chunk_ids": ["ch1"],
                        "learning_objective": "旧值",
                        "target_difficulty": "APPLICATION",  # V2.5 改名后禁止
                        "card_type": "QUESTION",
                        "coverage_tier": "CORE",
                    }
                ]
            },
            schema,
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "units": [
                    {
                        "source_chunk_ids": ["ch1"],
                        "learning_objective": "缺标签",
                        "target_difficulty": "BASIC",
                        "card_type": "QUESTION",
                    }
                ]
            },
            schema,
        )


def test_planner_schema_v3_units_require_grounded_source_chunk_ids() -> None:
    """source chunk ID 接地：单元必须引用 ≥1 个本次调用页 chunk_id 且不重复。"""
    schema = _schema("planner_output")
    source = schema["properties"]["units"]["items"]["properties"]["source_chunk_ids"]
    assert source["minItems"] == 1
    assert source["uniqueItems"] is True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "units": [
                    {
                        "source_chunk_ids": [],
                        "learning_objective": "x",
                        "target_difficulty": "BASIC",
                        "card_type": "QUESTION",
                        "coverage_tier": "CORE",
                    }
                ]
            },
            schema,
        )


def test_generator_schema_v3_keeps_zero_or_one_card_wrap() -> None:
    schema = _schema("generator_output")
    cards = schema["properties"]["cards"]
    assert cards["minItems"] == 0
    assert cards["maxItems"] == 1
    jsonschema.validate({"cards": []}, schema)
    jsonschema.validate({"cards": [{"type": "QUESTION", "question": "q", "answer": "a"}]}, schema)


def test_scoring_schema_v3_keeps_four_dimension_integer_scores() -> None:
    schema = _schema("scoring_output")
    score = schema["properties"]["scores"]["items"]
    assert "rubric_total_score" not in score["properties"]
    assert score["properties"]["evidence_score"]["maximum"] == 3
    assert score["properties"]["learning_value_score"]["maximum"] == 3


def test_planner_v4_coverage_modes_select_semantic_scope() -> None:
    """覆盖模式选语义范围不选数量：三种模式映射标签范围，稀疏章节允许有限重复。"""
    text = _asset("prompts", "planner")
    assert "coverage_mode" in text
    for mode in COVERAGE_MODES:
        assert mode in text
    assert "CORE" in text and "IMPORTANT" in text and "LOW_FREQUENCY" in text
    assert "学习角度" in text  # 知识稀疏时允许不同学习角度
    assert "同义重复" in text  # 禁止同义重复


def test_planner_v4_source_grounding_instructions() -> None:
    text = _asset("prompts", "planner")
    assert "chunk_id" in text
    assert "只能引用" in text
    assert "训练记忆" in text


def test_generator_v4_deep_question_maps_to_reference_approach() -> None:
    """Generator 仅对开放深问映射参考解法背面：不声称唯一标准答案。"""
    text = _asset("prompts", "generator")
    assert "DEEP_QUESTION" in text
    assert "参考思路" in text or "参考解法" in text
    assert "唯一标准答案" in text


def test_rubric_v3_deep_question_scoring_semantics() -> None:
    text = _asset("rubrics", "main")
    assert "DEEP_QUESTION" in text
    assert "参考思路" in text or "参考解法" in text


def test_assets_forbid_count_cost_pause_semantics() -> None:
    """禁止 count/cost/pause 语义：prompts/rubrics 当前资产不得含数量承诺、费用与暂停词。"""
    current = [
        ("prompts", "planner"),
        ("prompts", "generator"),
        ("prompts", "rewrite"),
        ("prompts", "scoring"),
        ("rubrics", "main"),
    ]
    for section, name in current:
        text = _asset(section, name)
        for token in _FORBIDDEN_TOKENS:
            assert token not in text, f"{section}/{name} 含禁止语义词 {token!r}"


def test_v4_v3_assets_do_not_mention_legacy_application() -> None:
    """当前资产不含旧难度名 APPLICATION（V2.5 改名收敛；历史说明只留在 CHANGELOG）。"""
    current = [
        ("prompts", "planner"),
        ("prompts", "generator"),
        ("prompts", "rewrite"),
        ("prompts", "scoring"),
        ("rubrics", "main"),
        ("schemas", "planner_output"),
        ("schemas", "generator_output"),
        ("schemas", "scoring_output"),
    ]
    for section, name in current:
        assert "APPLICATION" not in _asset(section, name), f"{section}/{name} 含旧难度名"
