"""单卡 AI 重写预览 schema（openapi CardRewritePreview；structure-contract 3.19，V2.5 新增）。"""

from pydantic import BaseModel


class CardRewritePreview(BaseModel):
    rewrite_id: str
    card_id: str
    base_card_version: str  # 应用时乐观并发校验（CAS）
    front: str
    back: str
    card_type: str  # QUESTION/TRUE_FALSE
    target_difficulty: str | None  # BASIC/UNDERSTANDING/DEEP_QUESTION
    custom_requirements: str | None = None  # 不保存完整 Prompt
    status: str  # PENDING/APPLIED/CANCELLED/EXPIRED
    expires_at: str  # 24 小时（实现常量统一）
    created_at: str
