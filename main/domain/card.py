"""Card（structure-contract 3.9）。

V2.5：STAGED/PUBLISHED 发布态与统一可见谓词（3.9）——所有列表、到期队列、
今日计划、统计与进度聚合复用同一查询谓词，禁止各模块自行漏写过滤。
"""

# 历史卡发布态回填值（database-design 7.3：历史卡均迁为 PUBLISHED）。
LEGACY_CARD_PUBLICATION_STATE = "PUBLISHED"

# 统一可见谓词（structure-contract 3.9）：卡片可见条件恒为
# publication_state = 'PUBLISHED' AND delete_batch_id IS NULL。
VISIBLE_PREDICATE_SQL = "publication_state = 'PUBLISHED' AND delete_batch_id IS NULL"

# 谓词的绑定参数形态（SQLAlchemy where 复用）。
VISIBLE_PREDICATE_SQL_PARAMS: dict[str, object] = {
    "publication_state": "PUBLISHED",
    "delete_batch_id": None,
}
