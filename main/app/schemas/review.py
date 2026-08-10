"""复习相关 schema（openapi ReviewState/ReviewEventRequest/ReviewQueueItem；structure-contract 3.10/3.11）。

ReviewQueueItem 平铺模型：openapi 为 allOf（Card + review_state）——用 Card 继承实现平铺
（Pydantic 字段继承，卡字段 + review_state 同层），与契约守卫校验口径一致。
ReviewEventRequest.rating 用 str（非 Literal）：非法评级由 service 内 rating_from_str
抛 REVIEW_EVENT_INVALID（400，契约第 7 章权威）；Literal 会被 FastAPI 拦截为 422。
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


class ReviewEventRequest(BaseModel):
    card_id: str
    rating: str  # AGAIN/HARD/GOOD/EASY（str + service 内 rating_from_str 校验 → 400 REVIEW_EVENT_INVALID）
    client_event_id: str
    device_timezone: str


class ReviewQueueItem(Card):
    review_state: ReviewState
