"""decks.py：牌组请求/响应模型（openapi Deck；structure-contract 3.8 派生进度）。

V2.5：Deck.project_id 归属学习项目（null = 手动/独立牌组）；source 枚举补 GENERATED。
"""

from pydantic import BaseModel, Field


class Deck(BaseModel):
    deck_id: str
    name: str
    source: str  # MANUAL/IMPORTED/GENERATED
    project_id: str | None = None  # V2.5 归属学习项目；null = 手动/独立牌组
    card_count: int
    due_count: int
    mastered_card_count: int
    review_count: int
    mastery_ratio: float
    not_started_count: int = 0
    learning_count: int = 0
    relearning_count: int = 0
    consolidating_count: int = 0
    mastered_count: int = 0
    review_event_count: int = 0
    last_studied_at: str | None = None
    created_at: str
    updated_at: str
    version: str


class DeckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    project_id: str | None = None  # V2.5 可选归属项目（后续任务使用）


class DeckUpdateRequest(BaseModel):
    """牌组改名（openapi DeckUpdateRequest；structure-contract 6.5）。"""

    name: str = Field(min_length=1, max_length=64)
