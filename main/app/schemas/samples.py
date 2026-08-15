"""样卡 schema（openapi SampleCard/GenerationConfig/DifficultyRatio；structure-contract 3.5/3.13）。

GenerationConfig 为 samples/tasks 共享请求模型（openapi 组件级定义），tasks.py 复用。
V2.5：DifficultyRatio 三档为 0~100 的 10% 整数档、合计 100、允许任一档为 0；
比例全 0 为非法配置（契约 3.5/4.1，INVALID_PREFERENCES 语义）。
SampleRequest 为旧 /samples 兼容路径请求模型（V2.5 openapi 无命名 schema，不锚定守卫）。
"""

from pydantic import BaseModel, Field, model_validator

from domain.enums import CoverageMode


class DifficultyRatio(BaseModel):
    """难度整数比例（openapi DifficultyRatio；structure-contract 3.5）。

    约束：三档 0~100、10% 整数档、合计 100、允许任一档为 0、全 0 非法。
    """

    basic: int
    understanding: int
    deep_question: int

    @model_validator(mode="after")
    def _check_v25_ratio(self) -> "DifficultyRatio":
        values = (self.basic, self.understanding, self.deep_question)
        if any(v < 0 or v > 100 or v % 10 != 0 for v in values):
            raise ValueError("比例须为 0~100 的 10% 整数档")
        if all(v == 0 for v in values):
            raise ValueError("比例全 0 为非法配置")
        if sum(values) != 100:
            raise ValueError("三档比例合计必须为 100")
        return self


class GenerationConfig(BaseModel):
    """任务生成配置（openapi GenerationConfig；structure-contract 3.5）。

    V2.5：quantity_tendency 改名 coverage_mode（COMPACT/BALANCED/EXTENSIVE）。
    """

    coverage_mode: str  # COMPACT/BALANCED/EXTENSIVE（域校验见下方 validator）
    difficulty_ratio: DifficultyRatio
    custom_requirements: str | None = None

    @model_validator(mode="after")
    def _check_coverage_mode(self) -> "GenerationConfig":
        if self.coverage_mode not in {mode.value for mode in CoverageMode}:
            raise ValueError("非法 coverage_mode")
        return self


class SampleRequest(BaseModel):
    """旧 /samples 兼容路径请求（V2.5 起样卡持久化于任务，本模型仅过渡期使用）。"""

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
