"""任务 schema（openapi Task/TaskCreateRequest/TaskUpdateRequest/Chapter/KnowledgePoint；structure-contract 3.4/3.6/6.4）。

Task 视图：selected_chapters 为 Chapter 对象数组快照（契约 3.4/3.6，章节删除后名称可还原）。
KnowledgePoint 为内部资源（契约 3.6；本期无独立接口，经任务详情/批次观测间接呈现）——
视图模型作为守卫锚点（红线 1：app/schemas ↔ openapi 三处一致）。
V2.5：七态 + internal_stage + project_id/retry_of_task_id/样卡持久化字段；
TaskCreateRequest 的 project_id 取自路径（openapi 描述），file_id 由项目派生。
"""

from pydantic import BaseModel, Field

from app.schemas.samples import GenerationConfig, SampleCard


class Chapter(BaseModel):
    chapter_id: str
    name: str
    start_page: int
    end_page: int


class TaskCreateRequest(BaseModel):
    """建立 DRAFT 任务（openapi TaskCreateRequest；project_id 取自路径）。"""

    deck_id: str
    chapter_ids: list[str] = Field(min_length=1)
    generation_config: GenerationConfig


class TaskUpdateRequest(BaseModel):
    """更新任务配置（openapi TaskUpdateRequest；仅 DRAFT/AWAITING_SAMPLE_CONFIRMATION
    可改，修改后样卡失效）。"""

    deck_id: str | None = None
    chapter_ids: list[str] | None = None
    generation_config: GenerationConfig | None = None


class TaskCursor(BaseModel):
    completed_batch_count: int


class KnowledgePoint(BaseModel):
    """知识点视图（openapi KnowledgePoint；structure-contract 3.6，required 七字段）。

    内部资源、无独立端点：视图模型仅作为守卫锚点，V5 经任务详情/批次观测呈现时复用。
    """

    knowledge_point_id: str
    task_id: str
    chapter_id: str
    source_chunk_id: str
    topic: str
    priority: int
    status: str  # PENDING/PROCESSED/SKIPPED


class Task(BaseModel):
    """任务视图（openapi Task；structure-contract 3.4，V2.5 七态）。"""

    task_id: str
    project_id: str | None
    file_id: str | None
    deck_id: str | None
    retry_of_task_id: str | None = None
    operation_id: str | None = None
    status: str  # V2.5 七态（DRAFT/SAMPLE_GENERATING/AWAITING_SAMPLE_CONFIRMATION/GENERATING/COMPLETED/FAILED/ABANDONED）
    internal_stage: str | None = None  # PLANNING/GENERATING/SCORING/PUBLISHING（运行期观测）
    selected_chapters: list[Chapter]
    generation_config: GenerationConfig
    sample_cards: list[SampleCard] | None = None
    sample_config_hash: str | None = None
    sample_confirmed_at: str | None = None
    cursor: TaskCursor | None = None
    generated_card_count: int
    total_batch_count: int | None = None
    completed_batch_count: int | None = None
    completion_reason: str | None = None  # NO_GENERATION_UNITS 等空单元三分支（spec §6.4）
    skipped_planning_group_count: int  # 部分规划组失败被跳过的组数（spec §6.4）
    resumable: bool
    failure_stage: str | None = None  # PLANNING/GENERATING/SCORING/PUBLISHING
    error_code: str | None = None
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    updated_at: str


class Batch(BaseModel):
    """批次视图（structure-contract 3.7；openapi Batch；经 GET /tasks/{task_id}/batches 观测返回，契约 6.9/AC-07）。

    观测字段：状态/retry/质量分布/usage（FR-11 Prompt Cache）/版本/model/http_status/
    duration/request_id（当前恒 null，R1 live 时透传上游 id）；cost_estimate 按 8.4
    价格常量估算（仅观测，不落库）。
    """

    batch_id: str
    task_id: str
    batch_index: int
    status: str  # PENDING/PROCESSING/SUCCEEDED/FAILED/SKIPPED
    generated_item_ids: list[str] | None = None
    retry_count: int
    coverage_rate: float | None = None
    duplicate_rate: float | None = None
    difficulty_distribution: dict[str, int] | None = None
    chapter_distribution: dict[str, int] | None = None
    card_type_distribution: dict[str, int] | None = None
    difficulty_deviation: float | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    output_tokens: int | None = None
    request_id: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    rubric_version: str | None = None
    duration_ms: int | None = None
    http_status: int | None = None
    created_at: str | None = None
    ended_at: str | None = None
    cost_estimate: float | None = None
