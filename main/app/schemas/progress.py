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
