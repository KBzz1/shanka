"""今日学习计划 schema（openapi TodayStudyPlan/TodayPlanCard；structure-contract 3.20，V2.5 新增）。

主计划（契约 4.5）：当前项目全部已学习且到期的可见卡 → 遗忘风险 DESC → 逾期时长 DESC →
card_id 稳定排序取到每日目标；仍有余额时从项目学习范围中的 NEW 卡按章节顺序、position、
card_id 补足。
"""

from pydantic import BaseModel

from app.schemas.cards import Card
from app.schemas.review import ReviewState


class TodayPlanCard(Card):
    """今日计划卡（openapi TodayPlanCard = allOf(Card + review_state/forgetting_risk)）。"""

    review_state: ReviewState | None = None
    forgetting_risk: float | None = None  # 统一 FSRS 适配器按服务端 now 计算；无法计算时置 0
    plan_kind: str | None = None  # DUE/NEW；旧客户端可忽略


class StudyPlan(BaseModel):
    """当前用户的可编辑账号级学习计划（V25-D-39：可跨项目与独立卡组）。"""

    configured: bool
    selected_deck_ids: list[str]
    daily_new_goal: int
    daily_review_goal: int
    updated_at: str | None = None


class StudyPlanUpdateRequest(BaseModel):
    # The plan write contract is account-scoped (V25-D-39): no project field — decks may
    # span projects and standalone decks. Legacy clients sending project_id are tolerated
    # (pydantic ignores unknown fields) and the value is ignored.
    selected_deck_ids: list[str]
    daily_new_goal: int
    daily_review_goal: int


class TodayStudyPlan(BaseModel):
    timezone: str  # 账号学习时区
    study_date: str  # 账号学习时区下的学习日期
    daily_goal: int  # 旧合计字段；新客户端使用双目标字段
    today_completed_count: int  # 旧合计完成数
    due_count: int
    main_plan_remaining: int
    backlog_count: int  # 到期总数超出每日目标的部分
    cards: list[TodayPlanCard]
    daily_new_goal: int = 0
    daily_review_goal: int = 0
    new_completed_count: int = 0
    review_completed_count: int = 0
    new_remaining_count: int = 0
    review_remaining_count: int = 0
    core_target_count: int = 0
    plan_configured: bool = False
    selected_deck_ids: list[str] = []
