"""复习相关 schema（openapi ReviewState/ReviewEvent/ReviewEventRequest/ReviewQueueItem；structure-contract 3.10/3.11）。

ReviewQueueItem 平铺模型：openapi 为 allOf（Card + review_state）——用 Card 继承实现平铺
（Pydantic 字段继承，卡字段 + review_state 同层），与契约守卫校验口径一致。
ReviewEventRequest.rating 用 str（非 Literal）：非法评级由 service 内 rating_from_str
抛 REVIEW_EVENT_INVALID（400，契约第 7 章权威）；Literal 会被 FastAPI 拦截为 422。
V2.5：ReviewEventRequest 不再要求 device_timezone（可空审计字段，1.2）。
"""

from pydantic import BaseModel

from app.schemas.cards import Card


class ReviewState(BaseModel):
    review_state_id: str
    card_id: str
    state: str  # NEW/LEARNING/REVIEW/RELEARNING
    stability: float
    difficulty: float
    due: str
    last_review: str | None = None
    reps: int
    lapses: int
    last_rating: str | None = None  # AGAIN/HARD/GOOD/EASY
    updated_at: str


class ReviewEvent(BaseModel):
    """复习事件（openapi ReviewEvent；structure-contract 3.11，不可变记录）。

    视图模型作为守卫锚点（本期无查询接口，评级提交时服务端内部使用）。
    """

    review_event_id: str
    client_event_id: str
    card_id: str
    rating: str  # AGAIN/HARD/GOOD/EASY
    reviewed_at: str
    device_timezone: str | None = None  # V2.5 降级为可空审计字段，不参与权威统计
    created_at: str


class ReviewEventRequest(BaseModel):
    card_id: str
    rating: str  # AGAIN/HARD/GOOD/EASY（str + service 内 rating_from_str 校验 → 400 REVIEW_EVENT_INVALID）
    client_event_id: str
    # V2.5：不再要求 device_timezone（客户端不上报；服务端按账号学习时区统计）


class ReviewQueueItem(Card):
    review_state: ReviewState


class ReviewSubmitResponse(BaseModel):
    """评级响应（openapi /review-events 200 内联对象：required [review_state, study_date]）。"""

    review_state: ReviewState
    study_date: str  # 账号学习时区下的本次学习日期（契约 1.2/6.6）
