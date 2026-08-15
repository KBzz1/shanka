"""看板 schema（openapi StatsDashboard；structure-contract 3.12）。

openapi required 集合含全部字段（比率类字段以 null 表示分母 0）——模型字段全部必填但可空，
守卫校验必填集；weekly_activity 长度 7 由 service 保证（openapi minItems/maxItems 7）。
V2.5：weekly_completed_count（本周不同 (账号学习日期, card_id) 数，去重口径）；
weekly_goal / weekly_goal_progress 改为必填（服务端派生 = daily_learning_goal × 7，
不再接受客户端参数）。
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
    weekly_completed_count: int  # V2.5 本周不同 (账号学习日期, card_id) 数（去重）
    week_change_rate: float | None
    weekly_goal: int | None  # V2.5 服务端派生 = daily_learning_goal × 7
    weekly_goal_progress: float | None  # min(weekly_completed_count / weekly_goal, 1)
    recall_accuracy: float | None
    first_answer_accuracy: float | None
    retention_rate: float | None
    streak_days: int
    mastered_card_count: int
    updated_at: str
    has_data: bool
