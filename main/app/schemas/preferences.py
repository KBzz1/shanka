"""账号偏好 schema（openapi UserPreferences/UserPreferencesUpdateRequest；structure-contract 3.15，V2.5 新增）。

比例/每日目标/IANA 时区服务端校验（INVALID_PREFERENCES / INVALID_LEARNING_TIMEZONE）；
部分更新 last-success-wins；主题仍为客户端本机偏好，不进入此资源。
"""

from pydantic import BaseModel

from app.schemas.samples import DifficultyRatio


class UserPreferences(BaseModel):
    default_coverage_mode: str  # COMPACT/BALANCED/EXTENSIVE，默认 BALANCED
    default_difficulty_ratio: DifficultyRatio  # 0~100 的 10% 整数档，合计 100，默认 40/40/20
    daily_learning_goal: int  # 10~200，10 的倍数，默认 50
    learning_timezone: str  # 有效 IANA 时区，账号级权威
    current_project_id: str | None  # 当前学习项目；项目删除时服务端置空
    updated_at: str


class UserPreferencesUpdateRequest(BaseModel):
    """部分更新偏好（openapi UserPreferencesUpdateRequest；last-success-wins）。"""

    default_coverage_mode: str | None = None
    default_difficulty_ratio: DifficultyRatio | None = None
    daily_learning_goal: int | None = None
    learning_timezone: str | None = None
    current_project_id: str | None = None
