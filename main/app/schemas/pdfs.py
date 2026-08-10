"""PDF schema（openapi PdfFile/Chapter/ChapterUpdateRequest；structure-contract 3.2/3.3）。"""

from pydantic import BaseModel, Field


class Chapter(BaseModel):
    chapter_id: str
    name: str
    start_page: int
    end_page: int


class PdfFile(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    status: str  # PENDING/PARSING/PARSED/FAILED
    error_code: str | None = None
    chapters: list[Chapter] | None = None
    created_at: str


class ChapterUpdateRequest(BaseModel):
    """部分更新（openapi：「至少提供一个字段;未提供的字段保持不变」）。"""

    name: str | None = Field(default=None, min_length=1)
    start_page: int | None = Field(default=None, ge=1)
    end_page: int | None = Field(default=None, ge=1)
