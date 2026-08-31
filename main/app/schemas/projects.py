"""项目 API 专用请求模型（契约 schema 在 project.py，不重复定义——裁决）。

仅放置 openapi 之外的 API 载体：PATCH 重命名请求体与 POST /projects 内联对象；
multipart 上传（file）与查询参数（retain_decks/retain_cards）由 FastAPI 直接绑定。
字段与已转正契约一致（name 去首尾空白后 1~60 字符，可重名）。
"""

from pydantic import BaseModel, Field


class ProjectCreateRequest(BaseModel):
    """openapi POST /projects 内联请求体：两步创建第一步（仅名称的空项目）。"""

    name: str = Field(min_length=1, max_length=60)


class ProjectRenameRequest(BaseModel):
    """openapi PATCH /projects/{project_id} 内联请求体：name 1~60 字符（原始长度）。"""

    name: str = Field(min_length=1, max_length=60)


class TextMaterialCreateRequest(BaseModel):
    """openapi POST /projects/{id}/materials/text（契约 TextMaterialCreateRequest）。"""

    name: str = Field(min_length=1, max_length=60)
    content: str = Field(min_length=1, max_length=30000)
