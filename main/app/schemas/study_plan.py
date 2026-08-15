"""今日学习计划 schema（openapi TodayStudyPlan/TodayPlanCard；structure-contract 3.20，V2.5 新增）。

主计划（契约 4.5）：当前项目全部已学习且到期的可见卡 → 遗忘风险 DESC → 逾期时长 DESC →
card_id 稳定排序取到每日目标；仍有余额时从项目学习范围中的 NEW 卡按章节顺序、position、
card_id 补足。
"""

from pydantic import BaseModel

from app.schemas.cards import Card
from app.schemas.project import LearningProject
from app.schemas.review import ReviewState


class TodayPlanCard(Card):
    """今日计划卡（openapi TodayPlanCard = allOf(Card + review_state/forgetting_risk)）。"""

    review_state: ReviewState | None = None
    forgetting_risk: float | None = None  # 统一 FSRS 适配器按服务端 now 计算；无法计算时置 0


class TodayStudyPlan(BaseModel):
    timezone: str  # 账号学习时区
    study_date: str  # 账号学习时区下的学习日期
    current_project: LearningProject | None  # 无当前项目时返回 null（空态）
    daily_goal: int  # 服务端偏好
    today_completed_count: int  # 今日去重完成数（同一 (账号学习日期, card_id) 只计一次）
    due_count: int
    main_plan_remaining: int
    backlog_count: int  # 到期总数超出每日目标的部分
    cards: list[TodayPlanCard]
