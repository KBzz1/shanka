"""agent 资产加载与 Prompt 组装（Architecture AGENTS.md 6/红线 5）。

- manifest.json 为唯一版本入口：prompts.planner/generator、schemas.card、rubrics.main；
- 资产路径相对 agent_evolution/；加载时校验存在；
- Prompt = 稳定前缀（资产，系统指令）+ 动态后缀（topic/chapter/difficulty/custom/JSON schema）；
- 完整 Prompt 不落日志（红线 4/AC-08）。
"""

import json
from pathlib import Path
from typing import Any

from app.errors import AppError, ErrorCode

# 本文件位于 main/infra/llm/prompts.py：parents[0..2]=llm/infra/main，parents[3]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST_PATH = _REPO_ROOT / "agent_evolution" / "manifest.json"
_ASSETS_ROOT = _REPO_ROOT / "agent_evolution"


def load_manifest() -> dict[str, Any]:
    try:
        with _MANIFEST_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, dict)
        return data
    except Exception as exc:
        raise AppError(ErrorCode.INTERNAL_ERROR, "agent 资产 manifest 加载失败") from exc


def load_asset(section: str, name: str) -> str:
    manifest = load_manifest()
    try:
        entry = manifest[section][name]
        path = _ASSETS_ROOT / str(entry["path"])
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        raise AppError(ErrorCode.INTERNAL_ERROR, f"agent 资产加载失败: {section}/{name}") from exc


def asset_versions() -> dict[str, str]:
    manifest = load_manifest()
    return {
        "prompt_version": manifest["prompts"]["generator"]["version"],
        "schema_version": manifest["schemas"]["card"]["version"],
        "rubric_version": manifest["rubrics"]["main"]["version"],
    }


def build_generation_prompt(
    prompt_asset: str,
    *,
    topic: str,
    chapter_name: str,
    difficulty: str,
    custom_requirements: str | None,
    card_schema: str,
) -> str:
    """稳定前缀（资产）+ 动态后缀。返回完整 prompt（调用方保证不落日志）。"""
    parts = [prompt_asset.strip(), f"主题：{topic}", f"章节：{chapter_name}", f"难度：{difficulty}"]
    if custom_requirements:
        parts.append(f"自定义要求：{custom_requirements}")
    parts.append(f"请严格按以下 JSON Schema 输出：\n{card_schema}")
    return "\n".join(parts)
