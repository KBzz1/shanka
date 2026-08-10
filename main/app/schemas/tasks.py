"""任务 schema（openapi Task/TaskCreateRequest/Chapter；structure-contract 3.4/6.4）。

Task 视图：selected_chapters 为 Chapter 对象数组快照（契约 3.4/3.6，章节删除后名称可还原）。
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
