"""scoring_validator.py：评分输出校验（spec §5.4/§5.6/§8；Task 11）。

- scoring-output schema v2（load_schema_asset("scoring_output")）负责根包装/结构/必填/
  四维 0~3/禁止额外字段；代码负责 ID 集合守恒与派生总分（模型不输出总分——schema v2
  无 rubric_total_score 且 additionalProperties:false，prompt 亦要求不输出）。
- 兼容裁决（spec §8 权威补充 2）：plan 测试 raw 携带 rubric_total_score——预处理剥离
  声称的 total → schema 校验 → 代码计算四维和 → raw 携带 total 且 ≠ 计算值 → 拒绝；
  相等则输出计算值（四维之和），不信任概率模型加法。
- 违规 → AppError(GENERATION_FAILED)：缺项、多项、越权 ID、重复 ID 或总分失配 →
  整次评分 FAILED，不落部分分数（§5.4 原子语义）。
"""

from typing import Any

import jsonschema

from app.errors import AppError, ErrorCode
from infra.llm.prompts import load_schema_asset

_DIMENSIONS = ("evidence_score", "correctness_score", "difficulty_score", "learning_value_score")


def validate_scores(raw: dict[str, Any], requested_ids: set[str]) -> dict[str, dict[str, int]]:
    """评分响应校验：剥离声称总分 → schema v2 → ID 集合守恒 → 代码计算总分。

    返回 `{generation_item_id: {四维, rubric_total_score}}`（总分恒为服务端计算值）。
    任何违规抛 AppError(GENERATION_FAILED)（整次 FAILED，不落部分分数）。
    """
    if not requested_ids:
        raise AppError(ErrorCode.GENERATION_FAILED, "评分请求 ID 集合为空")
    # 1. 预处理：剥离模型声称的 rubric_total_score（schema v2 禁止该字段；
    #    plan 测试兼容——声称值与计算值失配 → 拒绝）
    claimed: dict[str, object] = {}
    scores = raw.get("scores") if isinstance(raw, dict) else None
    stripped: dict[str, Any] = {"scores": []}
    if isinstance(scores, list):
        items: list[object] = []
        for entry in scores:
            if not isinstance(entry, dict):
                items.append(entry)  # 非对象成员 → 交由 schema 拒绝
                continue
            copied = dict(entry)
            total = copied.pop("rubric_total_score", None)
            if total is not None:
                claimed[str(copied.get("generation_item_id", ""))] = total
            items.append(copied)
        stripped["scores"] = items
    # 2. scoring-output schema v2 原子校验（根包装/必填/四维 0~3/禁止额外字段）
    validator = jsonschema.Draft202012Validator(load_schema_asset("scoring_output"))
    if list(validator.iter_errors(stripped)):
        raise AppError(ErrorCode.GENERATION_FAILED, "评分输出格式非法")
    scored = [item for item in stripped["scores"] if isinstance(item, dict)]
    # 3. ID 集合守恒：无缺/无多/无重复/无越权（§5.4）
    ids = [str(item["generation_item_id"]) for item in scored]
    if len(ids) != len(requested_ids) or set(ids) != requested_ids:
        raise AppError(ErrorCode.GENERATION_FAILED, "评分结果 ID 与请求集合不一致")
    # 4. 派生总分（四维之和，代码计算）：声称值存在且 ≠ 计算值 → 拒绝
    out: dict[str, dict[str, int]] = {}
    for item in scored:
        gen_id = str(item["generation_item_id"])
        total = sum(int(item[dim]) for dim in _DIMENSIONS)
        if gen_id in claimed and claimed[gen_id] != total:
            raise AppError(ErrorCode.GENERATION_FAILED, "评分总分与四维之和失配")
        out[gen_id] = {
            **{dim: int(item[dim]) for dim in _DIMENSIONS},
            "rubric_total_score": total,
        }
    return out
