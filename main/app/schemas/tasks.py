"""任务 schema（openapi Task/TaskCreateRequest/Chapter/KnowledgePoint；structure-contract 3.4/3.6/6.4）。

Task 视图：selected_chapters 为 Chapter 对象数组快照（契约 3.4/3.6，章节删除后名称可还原）。
KnowledgePoint 为内部资源（契约 3.6；本期无独立接口，经任务详情/批次观测间接呈现）——
视图模型作为守卫锚点（红线 1：app/schemas ↔ openapi 三处一致）。
"""

from pydantic import BaseModel, Field

from app.schemas.samples import GenerationConfig


class Chapter(BaseModel):
    chapter_id: str
    name: str
    start_page: int
    end_page: int


class TaskCreateRequest(BaseModel):
    file_id: str
    deck_id: str
    chapter_ids: list[str] = Field(min_length=1)
    generation_config: GenerationConfig


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
    task_id: str
    file_id: str | None
    deck_id: str | None
    status: str  # PENDING/RUNNING/PAUSED/COMPLETED/FAILED/CANCELLED
    stage: str | None  # PLANNING/GENERATING
    selected_chapters: list[Chapter]
    generation_config: GenerationConfig
    cursor: TaskCursor | None
    generated_card_count: int
    total_batch_count: int | None
    completed_batch_count: int | None
    resumable: bool
    failure_stage: str | None  # PLANNING/GENERATING/WRITE_BACK
    error_code: str | None
    created_at: str
    started_at: str | None
    ended_at: str | None
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


class CostEstimateRequest(BaseModel):
    """价格预估请求(spec 4:与 TaskCreateRequest 同构子集——仅章节+配置;纯计算,豁免幂等键)。"""

    chapter_ids: list[str] = Field(min_length=1)
    generation_config: GenerationConfig


class CostEstimateResponse(BaseModel):
    """价格预估响应(区间估值,单位元 CNY;spec 4/6.x)。"""

    knowledge_point_count: int
    estimated_card_count: int
    price_low: float
    price_high: float
    currency: str
