"""样卡 schema（openapi SampleRequest/GenerationConfig/DifficultyRatio；structure-contract 3.5/6.3）。

GenerationConfig 为 samples/tasks 共享请求模型（openapi 组件级定义），tasks.py 复用。
DifficultyRatio 不做数值约束（openapi 无 minimum）：非法比例由 validate_config 统一判 400。
"""

from pydantic import BaseModel, Field


class DifficultyRatio(BaseModel):
    basic: float
    understanding: float
    application: float


class GenerationConfig(BaseModel):
    quantity_tendency: str  # COMPACT/BALANCED/EXTENSIVE（validate_config 校验）
    difficulty_ratio: DifficultyRatio
    custom_requirements: str | None = None


class SampleRequest(BaseModel):
    file_id: str
    chapter_ids: list[str] = Field(min_length=1)
    generation_config: GenerationConfig


class SampleCard(BaseModel):
    """样卡轻量组件（structure-contract 3.13；openapi SampleCard）。

    与 Card 的差异：删去落库/归属/版本语义字段——样卡不入库、不参与统计与
    Rubric（PRD 5.5 数据规则），仅承载前端预览所需结构。
    """

    card_id: str
    front: str
    back: str
    code: str | None = None
    card_type: str
    question: str | None = None
    answer: str | None = None
    statement: str | None = None
    answer_boolean: bool | None = None
    explanation: str | None = None
    target_difficulty: str | None = None
