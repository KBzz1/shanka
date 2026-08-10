"""看板 schema（openapi StatsDashboard；structure-contract 3.12）。

openapi required 集合含全部字段（比率类字段以 null 表示分母 0）——模型字段全部必填但可空，
守卫校验必填集；weekly_activity 长度 7 由 service 保证（openapi minItems/maxItems 7）。
"""

from pydantic import BaseModel


class Period(BaseModel):
    start: str
    end: str
    week_ordinal: int


class StatsDashboard(BaseModel):
    period: Period
    timezone: str
    weekly_activity: list[int]
    weekly_total: int
    week_change_rate: float | None
    weekly_goal: int | None
    weekly_goal_progress: float | None
    recall_accuracy: float | None
    first_answer_accuracy: float | None
    retention_rate: float | None
    streak_days: int
    mastered_card_count: int
    updated_at: str
    has_data: bool
