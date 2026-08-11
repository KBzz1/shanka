"""decks.py：牌组请求/响应模型（openapi Deck；structure-contract 3.8 派生进度）。"""

from pydantic import BaseModel, Field


class Deck(BaseModel):
    deck_id: str
    name: str
    source: str  # MANUAL/IMPORTED（GENERATED 属 V4，R-11）
    card_count: int
    due_count: int
    mastered_card_count: int
    review_count: int
    mastery_ratio: float
    created_at: str
    updated_at: str
    version: str


class DeckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class DeckUpdateRequest(BaseModel):
    """牌组改名（openapi DeckUpdateRequest；structure-contract 6.5）。"""

    name: str = Field(min_length=1, max_length=64)
