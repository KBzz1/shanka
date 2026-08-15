"""学习项目 schema（openapi LearningProject/ProjectStudySettings/ProjectStudySettingsUpdateRequest；
structure-contract 3.16/3.17，V2.5 新增）。

一个项目恰好对应一份当前 PDF（learning_projects.file_id 唯一外键权威）；
项目状态由 PDF 状态与 chapters_confirmed_at 确定，不建立可漂移的第二套状态列。
"""

from pydantic import BaseModel

from app.schemas.pdfs import PdfFile


class LearningProject(BaseModel):
    project_id: str
    name: str  # 去首尾空白后 1~60 字符，可重名
    file: PdfFile  # 当前 PDF；列表响应可只返回摘要
    status: str  # PARSING/PARSE_FAILED/AWAITING_CHAPTER_CONFIRMATION/READY
    chapter_count: int
    deck_count: int
    task_count: int
    created_at: str
    updated_at: str
    version: str  # 缓存刷新与并发检查


class ProjectStudySettings(BaseModel):
    """项目学习设置（openapi ProjectStudySettings；structure-contract 3.17）。"""

    selected_new_card_chapter_ids: list[str]  # 只限制新卡；空数组 = 暂无新卡范围
    include_unassigned: bool  # 是否包含 chapter_id=null 的新卡
    updated_at: str


class ProjectStudySettingsUpdateRequest(BaseModel):
    selected_new_card_chapter_ids: list[str] | None = None
    include_unassigned: bool | None = None
