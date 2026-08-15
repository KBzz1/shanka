"""Task/GenerationConfig（structure-contract 3.4/3.5）。

V2.5 常量：七态迁移映射与 APPLICATION→DEEP_QUESTION 改名映射（database-design 7.3），
由迁移与 tests/contract/test_domain_enums_guard 复用。
"""

# V2.5 七态迁移映射（database-design 7.3）：历史状态 → V2.5 状态。
# 覆盖 V2.4 全状态域，映射后只留下 V2.5 可表达状态（禁止 PAUSED 等残留）。
TASK_STATUS_V25_MIGRATION: dict[str, str] = {
    "PENDING": "DRAFT",
    "RUNNING": "GENERATING",
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELLED": "ABANDONED",
    "PAUSED": "FAILED",
}

# 历史 PAUSED 任务迁为 FAILED 时的占位失败码（database-design 7.3）。
LEGACY_PAUSED_TASK_ERROR_CODE = "LEGACY_PAUSED_TASK"

# 历史 target_difficulty='APPLICATION' → 'DEEP_QUESTION'（structure-contract 3.5/3.6；
# knowledge_points + cards 两处列，database-design 7.3）。
DIFFICULTY_V25_MIGRATION: dict[str, str] = {"APPLICATION": "DEEP_QUESTION"}
