"""学习项目 schema（openapi LearningProject/Material；structure-contract 3.16/3.2a）。

项目 = 资料集合（V25-D-29）：materials 承载资料摘要；状态由全部资料状态与
chapters_confirmed_at 聚合派生（EMPTY/PARSING/PARSE_FAILED/AWAITING/READY）。
"""

from pydantic import BaseModel

from app.schemas.pdfs import Chapter
from app.schemas.tasks import Task


class Material(BaseModel):
    """学习资料（openapi Material，契约 3.2a）：PDF 行状态取自 pdf_files，TEXT 恒 READY。"""

    material_id: str
    project_id: str
    type: str  # PDF/TEXT（LINK 预留）
    name: str
    status: str | None  # PDF: PENDING/PARSING/PARSED/FAILED；TEXT: READY
    error_code: str | None = None
    size_bytes: int | None = None
    char_count: int | None = None
    chapter: Chapter | None = None  # TEXT 单章节（openapi Chapter 视图）
    created_at: str


class LearningProject(BaseModel):
    project_id: str
    name: str  # 去首尾空白后 1~60 字符，可重名
    materials: list[Material]  # 资料集合摘要；空数组=空项目
    status: str  # EMPTY/PARSING/PARSE_FAILED/AWAITING_CHAPTER_CONFIRMATION/READY
    chapter_count: int
    deck_count: int
    task_count: int
    tasks: list[Task] | None = None
    chapters: list[Chapter] | None = None  # 详情返回的跨资料章节列表；列表响应省略
    created_at: str
    updated_at: str
    version: str  # 缓存刷新与并发检查
