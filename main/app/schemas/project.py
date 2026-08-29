"""学习项目 schema（openapi LearningProject/ProjectStudySettings/ProjectStudySettingsUpdateRequest；
structure-contract 3.16/3.17，V2.5 新增）。

一个项目恰好对应一份当前 PDF（learning_projects.file_id 唯一外键权威）；
项目状态由 PDF 状态与 chapters_confirmed_at 确定，不建立可漂移的第二套状态列。
"""

from pydantic import BaseModel

from app.schemas.pdfs import PdfFile
from app.schemas.tasks import Task


class LearningProject(BaseModel):
    project_id: str
    name: str  # 去首尾空白后 1~60 字符，可重名
    file: PdfFile  # 当前 PDF；列表响应可只返回摘要
    status: str  # PARSING/PARSE_FAILED/AWAITING_CHAPTER_CONFIRMATION/READY
    chapter_count: int
    deck_count: int
    task_count: int
    tasks: list[Task] | None = None
    created_at: str
    updated_at: str
    version: str  # 缓存刷新与并发检查


class ProjectStudySettings(BaseModel):
    """项目学习设置（openapi ProjectStudySettings；structure-contract 3.17）。"""

    selected_new_card_chapter_ids: list[str]  # 旧章节范围读取兼容
    include_unassigned: bool  # 旧章节范围读取兼容
    selected_deck_ids: list[str] = []  # 今日计划完整卡组范围
    daily_new_goal: int = 10
    daily_review_goal: int = 40
    updated_at: str


class ProjectStudySettingsUpdateRequest(BaseModel):
    selected_new_card_chapter_ids: list[str] | None = None  # 旧接口兼容
    include_unassigned: bool | None = None  # 旧接口兼容
    selected_deck_ids: list[str] | None = None
    daily_new_goal: int | None = None
    daily_review_goal: int | None = None
