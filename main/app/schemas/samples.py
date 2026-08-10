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
