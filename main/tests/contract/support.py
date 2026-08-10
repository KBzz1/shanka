"""契约守卫辅助：解析 openapi.yaml / structure-contract.md / manifest.json 与 pydantic 模型比对。

权威来源（防漂移规则）：docs/Architecture/* 与 agent_evolution/manifest.json；
本模块只做解析比对，不建立第二套错误码/字段权威。F0 起全包复用，纵向包按需扩展。
"""

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo

REPO_ROOT = Path(__file__).resolve().parents[3]

OPENAPI_PATH = REPO_ROOT / "docs" / "Architecture" / "openapi.yaml"
STRUCTURE_CONTRACT_PATH = REPO_ROOT / "docs" / "Architecture" / "structure-contract.md"
MANIFEST_PATH = REPO_ROOT / "agent_evolution" / "manifest.json"

# openapi type → 期望 Python 注解（字符串枚举单独处理）
_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}


def load_openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as f:
        return cast(dict[str, Any], yaml.safe_load(f))


def openapi_schema(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], load_openapi()["components"]["schemas"][name])


def resolve_ref(schema: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    """解析 `$ref`（仅组件内引用），返回实际 schema。"""
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert isinstance(ref, str) and ref.startswith("#/components/schemas/")
    name = ref.removeprefix("#/components/schemas/")
    return cast(dict[str, Any], openapi["components"]["schemas"][name])


def check_schema_consistency(
    model: type[BaseModel], schema: dict[str, Any], openapi: dict[str, Any], path: str = "$"
) -> list[str]:
    """递归比较 pydantic 模型与 openapi schema，返回违约列表（空 = 一致）。

    F0 覆盖：object 属性名与必填、string（含 enum）、integer/number/boolean/array 类型映射、
    $ref 解析；anyOf/oneOf/allOf 契约暂无，纵向包按需扩展。
    """
    violations: list[str] = []
    schema = resolve_ref(schema, openapi)
    model_fields: dict[str, FieldInfo] = model.model_fields
    props: dict[str, Any] = schema.get("properties", {})
    for name in props:
        if name not in model_fields:
            violations.append(f"{path}: openapi 属性 {name!r} 在模型中缺失")
    for name in model_fields:
        if name not in props:
            violations.append(f"{path}: 模型字段 {name!r} 在 openapi 中缺失")
    required = set(schema.get("required", []))
    for name in required:
        field = model_fields.get(name)
        if field is not None and not field.is_required():
            violations.append(f"{path}.{name}: openapi 必填但模型可选")
    for name, prop in props.items():
        if name not in model_fields:
            continue
        annotation: Any = model_fields[name].annotation
        resolved = resolve_ref(prop, openapi)
        prop_type: Any = resolved.get("type")
        if prop_type == "object":
            violations.extend(
                check_schema_consistency(annotation, resolved, openapi, f"{path}.{name}")
            )
            continue
        expected = _TYPE_MAP.get(prop_type)
        if expected is None:
            violations.append(f"{path}.{name}: 未支持的 openapi type {prop_type!r}")
            continue
        if not _annotation_matches(annotation, expected):
            violations.append(f"{path}.{name}: openapi {prop_type!r} 与注解 {annotation!r} 不匹配")
        if prop_type == "string" and "enum" in resolved and _is_enum(annotation):
            member_values = {member.value for member in annotation}
            if member_values != set(resolved["enum"]):
                violations.append(f"{path}.{name}: openapi enum 与模型枚举不一致")
    return violations


def _is_enum(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def _annotation_matches(annotation: Any, expected: Any) -> bool:
    if expected is str:
        return annotation is str or _is_enum(annotation)
    if expected is list:
        return getattr(annotation, "__origin__", None) is list
    return annotation is expected


def parse_error_codes_table(md_text: str) -> dict[str, int]:
    """解析 structure-contract 第 7 章错误码表 → {CODE: http_status}。"""
    section = md_text.split("## 7. 错误码表", 1)[1].split("## 8.", 1)[0]
    result: dict[str, int] = {}
    # 数据行带分组前缀列（`| 通用 | `CODE` | 400 |` 或空分组 `| | `CODE` | 400 |`）；
    # 表头/分隔/注行无错误码单元（反引号），不会误匹配
    for line in section.splitlines():
        match = re.match(r"^\|\s*[^|]*\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*(\d{3})\s*\|", line)
        if match:
            result[match.group(1)] = int(match.group(2))
    return result


def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        return cast(dict[str, Any], json.load(f))


def extract_version_declarations(md_text: str) -> dict[str, str]:
    """structure-contract 中显式声明的 prompt_version/schema_version/rubric_version（当前无，
    仅 8.5 引用 manifest 对应 version）。发现声明时守卫要求与 manifest 一致（Architecture AGENTS.md 6）。"""
    result: dict[str, str] = {}
    for key in ("prompt_version", "schema_version", "rubric_version"):
        for match in re.finditer(rf"\b{key}\b[^\w]*?(v\d+)", md_text):
            result[key] = match.group(1)
            break
    return result
