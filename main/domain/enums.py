"""全部枚举：状态、评级、类型、来源（structure-contract 第 3 章资源模型；V2.5 权威）。

枚举值域以 openapi.yaml 组件枚举与 structure-contract 状态机为唯一来源；
值集一致性由 tests/contract/test_domain_enums_guard 守卫校验。
V2.5 落地：任务七态（4.1）、internal_stage 四段、APPLICATION→DEEP_QUESTION 改名、
STAGED/PUBLISHED 发布态、新资源枚举（项目状态/删除批次/重写预览/预设头像）。
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    """GenerationTask 七态（structure-contract 4.1；openapi TaskStatus）。

    V2.5：历史 PENDING→DRAFT、RUNNING→GENERATING、CANCELLED→ABANDONED、
    PAUSED→FAILED（LEGACY_PAUSED_TASK 占位，见 domain/task.py 迁移映射）。
    """

    DRAFT = "DRAFT"
    SAMPLE_GENERATING = "SAMPLE_GENERATING"
    AWAITING_SAMPLE_CONFIRMATION = "AWAITING_SAMPLE_CONFIRMATION"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"


class TaskInternalStage(StrEnum):
    """运行期内部阶段观测（structure-contract 3.4/4.1；不直接作为用户状态）。"""

    PLANNING = "PLANNING"
    GENERATING = "GENERATING"
    SCORING = "SCORING"
    PUBLISHING = "PUBLISHING"


class FailureStage(StrEnum):
    """失败阶段（structure-contract 3.4；openapi FailureStage）。"""

    PLANNING = "PLANNING"
    GENERATING = "GENERATING"
    SCORING = "SCORING"
    PUBLISHING = "PUBLISHING"


class CoverageMode(StrEnum):
    """覆盖深度（structure-contract 3.5；原 quantity_tendency 改名，V2.5）。"""

    COMPACT = "COMPACT"
    BALANCED = "BALANCED"
    EXTENSIVE = "EXTENSIVE"


class Difficulty(StrEnum):
    """难度（structure-contract 3.5/3.6；V2.5 改名：原 APPLICATION → DEEP_QUESTION）。"""

    BASIC = "BASIC"
    UNDERSTANDING = "UNDERSTANDING"
    DEEP_QUESTION = "DEEP_QUESTION"


class KnowledgePointStatus(StrEnum):
    """生成单元状态（structure-contract 3.6/4.2）。"""

    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    SKIPPED = "SKIPPED"


class BatchStatus(StrEnum):
    """批次状态（structure-contract 3.7/4.2）。"""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DeckSource(StrEnum):
    """牌组来源（structure-contract 3.8；V2.5 补 GENERATED）。"""

    MANUAL = "MANUAL"
    IMPORTED = "IMPORTED"
    GENERATED = "GENERATED"


class CardSource(StrEnum):
    """卡片来源（structure-contract 3.9）。"""

    GENERATED = "GENERATED"
    MANUAL = "MANUAL"
    IMPORTED = "IMPORTED"


class CardType(StrEnum):
    """卡型（structure-contract 3.6/3.9；决策 D-01）。"""

    QUESTION = "QUESTION"
    TRUE_FALSE = "TRUE_FALSE"


class ReviewStateValue(StrEnum):
    """FSRS 复习状态（structure-contract 3.10/4.3）。"""

    NEW = "NEW"
    LEARNING = "LEARNING"
    REVIEW = "REVIEW"
    RELEARNING = "RELEARNING"


class Rating(StrEnum):
    """评级（structure-contract 2/5.2）。"""

    AGAIN = "AGAIN"
    HARD = "HARD"
    GOOD = "GOOD"
    EASY = "EASY"


class ApiKeyStatus(StrEnum):
    """API Key 状态（structure-contract 3.1/6.2）。"""

    AVAILABLE = "AVAILABLE"
    INVALID = "INVALID"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    UNKNOWN = "UNKNOWN"


class PublicationState(StrEnum):
    """卡片发布态（structure-contract 3.9；V2.5）：STAGED 卡对任何用户侧查询不可见。"""

    STAGED = "STAGED"
    PUBLISHED = "PUBLISHED"


class ProjectStatus(StrEnum):
    """学习项目状态（structure-contract 3.16；V2.5 新增；由 PDF 状态与
    chapters_confirmed_at 确定，不建第二套状态列）。"""

    PARSING = "PARSING"
    PARSE_FAILED = "PARSE_FAILED"
    AWAITING_CHAPTER_CONFIRMATION = "AWAITING_CHAPTER_CONFIRMATION"
    READY = "READY"


class DeletionBatchStatus(StrEnum):
    """卡片删除批次状态（structure-contract 3.18；V2.5 新增）。"""

    PENDING = "PENDING"
    UNDONE = "UNDONE"
    FINALIZED = "FINALIZED"


class RewritePreviewStatus(StrEnum):
    """单卡重写预览状态（structure-contract 3.19；V2.5 新增）。"""

    PENDING = "PENDING"
    APPLIED = "APPLIED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class AvatarKey(StrEnum):
    """预设头像（structure-contract 3.14；V2.5）：mood_01~mood_12，只接受内置预设。"""

    MOOD_01 = "mood_01"
    MOOD_02 = "mood_02"
    MOOD_03 = "mood_03"
    MOOD_04 = "mood_04"
    MOOD_05 = "mood_05"
    MOOD_06 = "mood_06"
    MOOD_07 = "mood_07"
    MOOD_08 = "mood_08"
    MOOD_09 = "mood_09"
    MOOD_10 = "mood_10"
    MOOD_11 = "mood_11"
    MOOD_12 = "mood_12"

    @classmethod
    def default(cls) -> "AvatarKey":
        return cls.MOOD_01
