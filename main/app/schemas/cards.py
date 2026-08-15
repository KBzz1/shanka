"""cards.py：卡片请求/响应模型（openapi Card；structure-contract 3.9）。

V2.5 增量：source_task_id / chapter_id（生成来源任务与源章节）、
publication_state（STAGED/PUBLISHED）、delete_batch_id + pending_delete_at/undo_until
（10 秒撤销删除批次）。
"""

from typing import Literal

from pydantic import BaseModel, Field


class Card(BaseModel):
    card_id: str
    deck_id: str
    source: str  # GENERATED/MANUAL/IMPORTED
    position: int
    front: str
    back: str
    code: str | None = None
    card_type: str  # QUESTION/TRUE_FALSE
    question: str | None = None
    answer: str | None = None
    statement: str | None = None
    answer_boolean: bool | None = None
    explanation: str | None = None
    generation_item_id: str | None = None
    source_task_id: str | None = None  # V2.5 生成来源任务；删历史保留卡时置空
    chapter_id: str | None = None  # V2.5 源章节；null 显示"未归属章节"
    publication_state: str = "PUBLISHED"  # V2.5 STAGED/PUBLISHED；历史卡均迁为 PUBLISHED
    delete_batch_id: str | None = None  # V2.5 非空 = 10 秒待删除批次
    pending_delete_at: str | None = None  # V2.5 服务端计时
    undo_until: str | None = None  # V2.5 服务端撤销窗口
    target_difficulty: str | None = None
    knowledge_point_ids: list[str] | None = None
    evidence_score: int | None = None
    correctness_score: int | None = None
    difficulty_score: int | None = None
    learning_value_score: int | None = None
    rubric_total_score: int | None = None
    version: str
    created_at: str
    updated_at: str


class CardCreate(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class CardUpdateRequest(BaseModel):
    """编辑卡片（openapi CardUpdateRequest；structure-contract 6.5）。

    内容覆盖 + ReviewState 重置为新卡（2026-08-11 用户决策：与单卡重写同语义，
    内容已变则旧记忆不适用）；version 递增供缓存刷新。
    """

    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class ImportCard(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class ImportResult(BaseModel):
    index: int
    status: Literal["CREATED", "FAILED"]
    card_id: str | None = None
    error: dict[str, str] | None = None


class ImportResponse(BaseModel):
    results: list[ImportResult]
