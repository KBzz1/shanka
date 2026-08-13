"""契约守卫：agent_evolution/manifest.json ↔ structure-contract 版本引用（Architecture AGENTS.md 6）。"""

import json
import re

from tests.contract.support import (
    MANIFEST_PATH,
    STRUCTURE_CONTRACT_PATH,
    extract_version_declarations,
    load_manifest,
)


def test_manifest_asset_versions_and_paths_valid() -> None:
    manifest = load_manifest()
    assets = [
        ("prompts", "planner"),
        ("prompts", "generator"),
        ("prompts", "rewrite"),
        ("prompts", "scoring"),
        ("schemas", "card"),
        ("schemas", "generator_output"),
        ("schemas", "planner_output"),
        ("schemas", "scoring_output"),
        ("rubrics", "main"),
    ]
    for section, name in assets:
        entry = manifest[section][name]
        assert re.fullmatch(r"v\d+", entry["version"]), f"{section}.{name} 版本格式非法"
        asset_path = MANIFEST_PATH.parent / entry["path"]
        assert asset_path.is_file(), f"{section}.{name} 资产文件缺失: {asset_path}"


def test_manifest_versions_match_structure_contract_declarations() -> None:
    manifest = load_manifest()
    prompt_versions = {entry["version"] for entry in manifest["prompts"].values()}
    expected = {
        "prompt_version": prompt_versions,
        "schema_version": {entry["version"] for entry in manifest["schemas"].values()},
        "rubric_version": {manifest["rubrics"]["main"]["version"]},
    }
    declared = extract_version_declarations(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    for key, versions in expected.items():
        if key in declared:
            assert declared[key] in versions, f"{key}: 契约声明 {declared[key]} 与 manifest 不一致"


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
