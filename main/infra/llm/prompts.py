"""agent 资产加载、版本观测与安全 Prompt 数据序列化（红线 5）。

- manifest.json 为唯一版本入口：Planner/Generator/Rewrite/Scoring Prompt、输出 Schema 与
  Rubric；
- 资产路径相对 agent_evolution/；加载时校验存在；
- v3 动态输入由 ``safe_json_dumps`` 确定性序列化并转义信封边界字符；
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
    """manifest 全部资产版本（spec §5.1 版本布局）。

    扩展键供调用级观测（Task 7 起 llm_call_attempts 按调用记录具体 asset name/version，
    避免用一个 schema_version 混写 card v1 与三个 output schema，版本以 manifest 为准）；
    保留兼容键
    prompt_version（=generator prompt）与 schema_version（=generator-output schema，
    Batch.schema_version 语义）供既有消费者。
    """
    manifest = load_manifest()
    prompts = manifest["prompts"]
    schemas = manifest["schemas"]
    return {
        "generator_prompt_version": prompts["generator"]["version"],
        "planner_prompt_version": prompts["planner"]["version"],
        "rewrite_prompt_version": prompts["rewrite"]["version"],
        "scoring_prompt_version": prompts["scoring"]["version"],
        "card_schema_version": schemas["card"]["version"],
        "planner_output_schema_version": schemas["planner_output"]["version"],
        "scoring_output_schema_version": schemas["scoring_output"]["version"],
        "rubric_version": manifest["rubrics"]["main"]["version"],
        # 兼容键（旧消费者）
        "prompt_version": prompts["generator"]["version"],
        "schema_version": schemas["generator_output"]["version"],
    }


def load_schema_asset(name: str) -> dict[str, Any]:
    """加载 manifest schemas 节 JSON Schema 资产并解析为 dict（spec §5.6 输出校验层）。

    供 planner/generator/scoring validator 加载原始 output schema（版本以 manifest 为准；
    本文件不承担业务校验，只负责版本化加载与解析失败兜底）。
    """
    try:
        data = json.loads(load_asset("schemas", name))
        assert isinstance(data, dict)
        return data
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            ErrorCode.INTERNAL_ERROR, f"agent 资产 Schema 解析失败: schemas/{name}"
        ) from exc


def safe_json_dumps(payload: object) -> str:
    """确定性序列化不可信 Prompt 数据，并转义可伪造输入信封边界的字符。"""
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return rendered.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def build_generation_prompt(
    prompt_asset: str,
    *,
    topic: str,
    chapter_name: str,
    difficulty: str,
    custom_requirements: str | None,
    card_schema: str,
) -> str:
    """旧链路兼容 builder；v3 正式链路按设计使用稳定 system + 动态 user。"""
    parts = [prompt_asset.strip(), f"主题：{topic}", f"章节：{chapter_name}", f"难度：{difficulty}"]
    if custom_requirements:
        parts.append(f"自定义要求：{custom_requirements}")
    parts.append(f"请严格按以下 JSON Schema 输出：\n{card_schema}")
    return "\n".join(parts)
