"""validate.py：GenerationConfig 校验（3.5，V2.5 语义）。

V2.5：coverage_mode 值域（COMPACT/BALANCED/EXTENSIVE，原 quantity_tendency 改名）；
difficulty_ratio 三档为 0~100 的 10% 整数档、合计 100、允许任一档为 0；
比例全 0 为非法配置（契约 3.5/4.1：INVALID_PREFERENCES 语义）。
Pydantic 模型层已做结构约束（samples.DifficultyRatio validator），本函数为
service 层兜底校验（直接构造 dict 绕过模型的路径）。
"""

from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig

_VALID_COVERAGE_MODES = {"COMPACT", "BALANCED", "EXTENSIVE"}


def validate_config(config: GenerationConfig) -> None:
    """V2.5 配置校验：coverage_mode 值域 + 比例语义（非法 → INVALID_PREFERENCES 400）。"""
    mode = config.coverage_mode
    if mode not in _VALID_COVERAGE_MODES:
        raise AppError(ErrorCode.INVALID_PREFERENCES, "非法 coverage_mode")
    ratio = config.difficulty_ratio
    values = (ratio.basic, ratio.understanding, ratio.deep_question)
    if any(v < 0 or v > 100 or v % 10 != 0 for v in values):
        raise AppError(ErrorCode.INVALID_PREFERENCES, "比例须为 0~100 的 10% 整数档")
    if all(v == 0 for v in values):
        raise AppError(ErrorCode.INVALID_PREFERENCES, "比例全 0 为非法配置")
    if sum(values) != 100:
        raise AppError(ErrorCode.INVALID_PREFERENCES, "三档比例合计必须为 100")
