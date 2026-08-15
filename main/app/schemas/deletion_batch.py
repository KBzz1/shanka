"""卡片删除批次 schema（openapi CardDeletionBatch；structure-contract 3.18，V2.5 新增）。"""

from pydantic import BaseModel


class CardDeletionBatch(BaseModel):
    delete_batch_id: str
    card_ids: list[str]  # 服务端返回；数据库以 Cards 外键关系为权威
    undo_until: str  # 服务端接受最后一次追加后 10 秒
    status: str  # PENDING/UNDONE/FINALIZED
    created_at: str
    updated_at: str
