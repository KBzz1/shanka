"""Task/GenerationConfig（structure-contract 3.4/3.5）。

V2.5 常量：七态迁移映射与 APPLICATION→DEEP_QUESTION 改名映射（database-design 7.3），
由迁移与 tests/contract/test_domain_enums_guard 复用；活跃/终态任务状态集合（4.1
删除保护与删除语义）由服务层共享。
"""

from domain.enums import TaskStatus

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

# 活跃（非终态）任务状态（structure-contract 4.1 删除保护）：DRAFT/SAMPLE_GENERATING/
# AWAITING_SAMPLE_CONFIRMATION/GENERATING——项目/牌组/章节删除保护的统一口径。
# V2.5 起运行期只写七态，集合不含迁移期旧态（PENDING/RUNNING/PAUSED）。
ACTIVE_TASK_STATUSES: frozenset[str] = frozenset(
    status.value
    for status in (
        TaskStatus.DRAFT,
        TaskStatus.SAMPLE_GENERATING,
        TaskStatus.AWAITING_SAMPLE_CONFIRMATION,
        TaskStatus.GENERATING,
    )
)

# 终态任务状态（6.4 DELETE 仅终态可删）。
TERMINAL_TASK_STATUSES: frozenset[str] = frozenset(
    status.value for status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABANDONED)
)
