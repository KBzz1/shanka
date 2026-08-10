"""validate.py：GenerationConfig 校验（3.5）。"""

from typing import Any

from app.errors import AppError, ErrorCode

_VALID_TENDENCY = {"COMPACT", "BALANCED", "EXTENSIVE"}


def validate_config(config: dict[str, Any]) -> None:
    tendency = config.get("quantity_tendency")
    if tendency not in _VALID_TENDENCY:
        raise AppError(ErrorCode.VALIDATION_ERROR, "非法 quantity_tendency")
    ratio = config.get("difficulty_ratio") or {}
    try:
        total = sum(float(ratio.get(k, 0)) for k in ("basic", "understanding", "application"))
        ok = all(float(ratio.get(k, 0)) > 0 for k in ("basic", "understanding", "application"))
    except (TypeError, ValueError):
        ok = False
        total = 0.0
    if not ok or abs(total - 1.0) > 1e-9:
        raise AppError(ErrorCode.VALIDATION_ERROR, "difficulty_ratio 必须三值>0 且和为 1")
