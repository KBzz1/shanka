"""infra.llm.prompts 单元测试：manifest 加载/Prompt 组装。"""

import pytest

from app.errors import AppError, ErrorCode
from infra.llm.prompts import (
    asset_versions,
    build_generation_prompt,
    load_asset,
    load_manifest,
)


def test_prompts_load_manifest_valid() -> None:
    manifest = load_manifest()
    assert "prompts" in manifest and "schemas" in manifest and "rubrics" in manifest


def test_prompts_load_asset_exists() -> None:
    generator = load_asset("prompts", "generator")
    assert "generator" in generator or len(generator) > 50


def test_prompts_load_asset_missing_raises_internal_error() -> None:
    with pytest.raises(AppError) as excinfo:
        load_asset("prompts", "nonexistent")
    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR


def test_prompts_asset_versions_match_manifest() -> None:
    versions = asset_versions()
    assert versions["prompt_version"] == "v2"  # generator v2（R1 canary 修复）
    assert versions["schema_version"] == "v1"
    assert versions["rubric_version"] == "v1"


def test_prompts_build_generation_prompt_stable_and_dynamic() -> None:
    prefix = "你是闪卡生成助手。请根据以下内容生成卡片。"
    prompt = build_generation_prompt(
        prefix,
        topic="FSRS 间隔重复",
        chapter_name="第一章",
        difficulty="BASIC",
        custom_requirements="使用中文",
        card_schema='{"front": "string"}',
    )
    assert prompt.startswith(prefix)  # 稳定前缀保留
    assert "FSRS 间隔重复" in prompt  # 动态后缀
    assert "第一章" in prompt
    assert "BASIC" in prompt
    assert "使用中文" in prompt


def test_prompts_build_without_custom() -> None:
    prefix = "PREFIX"
    prompt = build_generation_prompt(
        prefix,
        topic="t",
        chapter_name="c",
        difficulty="BASIC",
        custom_requirements=None,
        card_schema="{}",
    )
    assert "PREFIX" in prompt and "t" in prompt and "custom" not in prompt.lower()
