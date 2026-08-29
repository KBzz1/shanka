"""真实学习进度投影（卡组/项目共用）。"""

from pydantic import BaseModel


class ProgressSummary(BaseModel):
    card_count: int
    not_started_count: int
    learning_count: int
    relearning_count: int
    consolidating_count: int
    mastered_count: int
    due_count: int
    review_event_count: int
    last_studied_at: str | None = None


class ProjectWeeklyStats(BaseModel):
    project_id: str
    period_start: str
    period_end: str
    timezone: str
    weekly_activity: list[int]
    weekly_total: int
    weekly_completed_count: int
    weekly_new_goal: int
    weekly_review_goal: int
    weekly_goal: int
    weekly_goal_progress: float | None
    new_completed_count: int
    review_completed_count: int
    updated_at: str
