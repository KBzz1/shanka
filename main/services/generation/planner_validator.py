"""planner_validator.py：Planner 输出校验与配额截断（spec §5.2/§5.6/§3.5）。

分层校验（schema 负责结构，代码负责来源/锚定/配额）：

- Schema：`load_schema_asset("planner_output")`（planner-output v2）负责根包装、结构、
  必填、枚举、范围和禁额外键；违反 → AppError(GENERATION_FAILED)（§6.3 输出非法）。
- 代码层逐单元校验：`source_chunk_ids` ⊆ 本次调用页集合；单元页数 ≤ max_pages_per_unit、
  来源字符和 ≤ max_chars_per_unit；违反 → AppError(GENERATION_FAILED)。
- 配额截断：按各难度子配额确定性截断——priority 升序保留、并列按原数组顺序（§3.5
  约束"Planner 若对某难度超配额，代码按输出数组相对顺序确定性截断，不重试"）。
- 规范化：`source_chunk_ids` 按页序重排（`page_chars` 插入序 = 调用方按
  `load_pages` 页序构造）、去重；priority 重排 1..N（服务端生成，模型不输出）。

输入容忍服务端 `priority` 提示键（模型契约不含数值 priority——§5.2；缺失时按数组
顺序即相对重要性截断）；其余任何额外键仍被 Schema 拒绝（§5.6 禁止过滤非法字段后
降格为合法结果）。
"""

from typing import Any

import jsonschema

from app.errors import AppError, ErrorCode
from infra.llm.prompts import load_schema_asset

_DIFFICULTY_ORDER = ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")


def _invalid(message: str) -> AppError:
    return AppError(ErrorCode.GENERATION_FAILED, message)


def validate_and_truncate(
    raw: dict[str, Any],
    *,
    allowed_page_ids: set[str],
    quota: dict[str, int],
    max_pages_per_unit: int,
    max_chars_per_unit: int,
    page_chars: dict[str, int],
) -> list[dict[str, Any]]:
    """Planner 原始输出 → 规范化且按配额截断后的 units（priority 1..N）。

    page_chars 契约：`{chunk_id: char_count}`，调用方必须按 `load_pages` 页序
    （page_number 升序）构造——规范化按该插入序重排来源，保证兼容投影首项确定
    （spec §5.2 按 page_number 规范化来源顺序）。
    """
    schema = load_schema_asset("planner_output")
    units = _schema_validate(raw, schema)
    for unit in units:
        _check_unit(
            unit,
            allowed_page_ids=allowed_page_ids,
            max_pages_per_unit=max_pages_per_unit,
            max_chars_per_unit=max_chars_per_unit,
            page_chars=page_chars,
        )
    kept = _truncate_by_quota(units, quota)
    page_order = {chunk_id: i for i, chunk_id in enumerate(page_chars)}
    return _normalize(kept, page_order=page_order)


def _schema_validate(raw: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Schema 校验（容忍服务端 priority 提示键；其余额外键拒绝）。"""
    if not isinstance(raw, dict) or not isinstance(raw.get("units"), list):
        raise _invalid("Planner 输出结构非法（顶层必须为含 units 数组的对象）")
    projected: list[Any] = []
    for unit in raw["units"]:
        if isinstance(unit, dict):
            unit = dict(unit)
            unit.pop("priority", None)
        projected.append(unit)
    errors = [
        f"{err.json_path or err.path}: {err.message}"
        for err in jsonschema.Draft202012Validator(schema).iter_errors({"units": projected})
    ]
    if errors:
        raise _invalid("Planner 输出不满足输出 Schema")
    return [u for u in raw["units"] if isinstance(u, dict)]


def _check_unit(
    unit: dict[str, Any],
    *,
    allowed_page_ids: set[str],
    max_pages_per_unit: int,
    max_chars_per_unit: int,
    page_chars: dict[str, int],
) -> None:
    """代码层逐单元校验：来源子集、页数上限、字符和上限（spec §5.2 生成输入上限）。"""
    chunk_ids = unit["source_chunk_ids"]
    if any(cid not in allowed_page_ids for cid in chunk_ids):
        raise _invalid("Planner 来源引用超出本次调用页集合")
    if len(chunk_ids) > max_pages_per_unit:
        raise _invalid("Planner 单元来源页数超出上限")
    if sum(page_chars.get(cid, 0) for cid in chunk_ids) > max_chars_per_unit:
        raise _invalid("Planner 单元来源字符数超出上限")


def _priority_key(unit: dict[str, Any], index: int) -> tuple[int, int]:
    """截断排序键：priority 提示升序、并列（或无提示）按原数组顺序（确定性）。"""
    hint = unit.get("priority")
    try:
        return (int(hint), index) if hint is not None else (index, index)
    except (TypeError, ValueError):
        return (index, index)


def _truncate_by_quota(units: list[dict[str, Any]], quota: dict[str, int]) -> list[dict[str, Any]]:
    """按难度配额确定性截断（§3.5）：每难度按 priority 升序保留 quota 个，输出保持原数组序。"""
    surviving: set[int] = set()
    for difficulty in _DIFFICULTY_ORDER:
        limit = quota.get(difficulty, 0)
        candidates = [
            (i, unit) for i, unit in enumerate(units) if unit["target_difficulty"] == difficulty
        ]
        candidates.sort(key=lambda pair: _priority_key(pair[1], pair[0]))
        surviving.update(i for i, _ in candidates[:limit])
    return [units[i] for i in sorted(surviving)]


def _normalize(units: list[dict[str, Any]], *, page_order: dict[str, int]) -> list[dict[str, Any]]:
    """规范化：source_chunk_ids 按页序重排 + 去重；priority 重排 1..N；只保留契约字段。"""
    normalized: list[dict[str, Any]] = []
    for unit in units:
        chunk_ids = list(
            dict.fromkeys(
                sorted(
                    unit["source_chunk_ids"],
                    key=lambda cid: page_order.get(cid, len(page_order)),
                )
            )
        )
        normalized.append(
            {
                "source_chunk_ids": chunk_ids,
                "learning_objective": unit["learning_objective"],
                "target_difficulty": unit["target_difficulty"],
                "card_type": unit["card_type"],
            }
        )
    return [{**unit, "priority": i + 1} for i, unit in enumerate(normalized)]


def normalize_units(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """无页上下文版本的规范化：source_chunk_ids 去重（保序）、priority 顺序化 1..N。

    页序重排需要 page_chars 上下文（见 validate_and_truncate）；本函数保持传入顺序，
    供合并阶段对已规范化单元做去重与全局 priority 重排（spec §6.2）。
    """
    normalized: list[dict[str, Any]] = []
    for unit in units:
        normalized.append(
            {
                **unit,
                "source_chunk_ids": list(dict.fromkeys(unit["source_chunk_ids"])),
            }
        )
    return [{**unit, "priority": i + 1} for i, unit in enumerate(normalized)]
