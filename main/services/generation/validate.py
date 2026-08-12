"""validate.py：GenerationConfig 校验（3.5）。"""

from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig

_VALID_TENDENCY = {"COMPACT", "BALANCED", "EXTENSIVE"}


def validate_config(config: GenerationConfig) -> None:
    tendency = config.quantity_tendency
    if tendency not in _VALID_TENDENCY:
        raise AppError(ErrorCode.VALIDATION_ERROR, "非法 quantity_tendency")
    ratio = config.difficulty_ratio
    total = ratio.basic + ratio.understanding + ratio.application
    if (
        not (ratio.basic > 0 and ratio.understanding > 0 and ratio.application > 0)
        or abs(total - 1.0) > 1e-9
    ):
        raise AppError(ErrorCode.VALIDATION_ERROR, "difficulty_ratio 必须三值>0 且和为 1")
