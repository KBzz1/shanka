"""契约守卫：agent_evolution/manifest.json ↔ structure-contract 版本引用（Architecture AGENTS.md 6）。"""

import json
import re

from tests.contract.support import (
    MANIFEST_PATH,
    STRUCTURE_CONTRACT_PATH,
    VERSION_KEYS,
    extract_version_declarations,
    load_manifest,
    manifest_version,
)


def test_manifest_asset_versions_and_paths_valid() -> None:
    manifest = load_manifest()
    assets = [
        ("prompts", "planner"),
        ("prompts", "planner_coarse"),
        ("prompts", "generator"),
        ("prompts", "rewrite"),
        ("prompts", "scoring"),
        ("prompts", "qa_planner"),
        ("prompts", "generator_qa"),
        ("schemas", "card"),
        ("schemas", "generator_output"),
        ("schemas", "planner_output"),
        ("schemas", "planner_coarse_output"),
        ("schemas", "scoring_output"),
        ("schemas", "qa_planner_output"),
        ("rubrics", "main"),
    ]
    for section, name in assets:
        entry = manifest[section][name]
        assert re.fullmatch(r"v\d+", entry["version"]), f"{section}.{name} 版本格式非法"
        asset_path = MANIFEST_PATH.parent / entry["path"]
        assert asset_path.is_file(), f"{section}.{name} 资产文件缺失: {asset_path}"


def test_manifest_versions_match_structure_contract_declarations() -> None:
    """红线 5：契约版本键（兼容键 + T5 扩展键共 10 键）与 manifest 对应资产逐键相等。

    契约当前不显式声明版本（8.5 只引用 manifest 为唯一权威）——一旦出现
    `<key> vN` 声明，必须与 manifest 中该资产版本完全一致。
    """
    manifest = load_manifest()
    expected = {key: manifest_version(manifest, path) for key, path in VERSION_KEYS}
    declared = extract_version_declarations(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    for key, version in expected.items():
        if key in declared:
            assert declared[key] == version, (
                f"{key}: 契约声明 {declared[key]} 与 manifest 资产版本 {version} 不一致"
            )


def test_manifest_json_parseable() -> None:
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    assert "prompts" in data and "schemas" in data and "rubrics" in data


def test_rewrite_prompt_asset_registered() -> None:
    """V6：manifest 注册 rewrite prompt，加载可得且可读（资产演进红线 5）。"""
    from infra.llm.prompts import load_asset

    text = load_asset("prompts", "rewrite")
    assert "重写" in text or "rewrite" in text  # 资产内容含重写指令
    assert "JSON Schema" in text  # 输出格式契约


def test_qa_assets_registered_and_versioned() -> None:
    """V25-D-43：问答直通资产（qa-planner/generator-qa prompt + 输出 schema）注册且可读。"""
    from infra.llm.prompts import asset_versions, load_asset

    planner = load_asset("prompts", "qa_planner")
    assert "问答对" in planner and "不改答案" in planner  # 提取语义：忠实资料问答
    generator_qa = load_asset("prompts", "generator_qa")
    assert "格式规范化" in generator_qa and "不写新题" in generator_qa  # 整理语义：仅排版
    load_asset("schemas", "qa_planner_output")  # 输出 schema 可读
    versions = asset_versions()
    assert versions["qa_planner_prompt_version"] == "v1"
    assert versions["generator_qa_prompt_version"] == "v1"
    assert versions["qa_planner_output_schema_version"] == "v1"
