"""契约守卫：Task/TaskCreateRequest/TaskUpdateRequest/KnowledgePoint/SampleCard ↔ openapi（守卫 1 扩展，红线 1）。

Task 视图的 selected_chapters 为 Chapter[]（object 数组快照，契约 3.4/3.6）、
generation_config 为 $ref GenerationConfig（object）——守卫 array-of-object / object
属性递归路径（F1 支持）。KnowledgePoint 为内部资源（契约 3.6，required 七字段）——
视图模型（app/schemas/tasks.py）作为守卫锚点。status/difficulty 等用 str 注解不校验
enum 值集（既有口径），值集一致性由 domain/enums 守卫（test_domain_enums_guard）承载。
SampleRequest 已随 V2.5 移出 openapi（旧 /samples 兼容路径无命名 schema），不再锚定。
"""

import pydantic
import pytest

from app.schemas.samples import DifficultyRatio, GenerationConfig, SampleCard
from app.schemas.tasks import KnowledgePoint, TaskCreateRequest, TaskUpdateRequest
from app.schemas.tasks import Task as TaskView
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_task_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(TaskView, openapi_schema("Task"), load_openapi())
    assert violations == []


def test_knowledge_point_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        KnowledgePoint, openapi_schema("KnowledgePoint"), load_openapi()
    )
    assert violations == []


def test_sample_card_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(SampleCard, openapi_schema("SampleCard"), load_openapi())
    assert violations == []


def test_task_create_request_schema_openapi_consistent() -> None:
    """V2.5：project_id 取自路径，请求体只含 deck_id/chapter_ids/generation_config。"""
    violations = check_schema_consistency(
        TaskCreateRequest, openapi_schema("TaskCreateRequest"), load_openapi()
    )
    assert violations == []


def test_task_update_request_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        TaskUpdateRequest, openapi_schema("TaskUpdateRequest"), load_openapi()
    )
    assert violations == []


def test_difficulty_ratio_accepts_v25_semantics() -> None:
    """V2.5：三档为 0~100 的 10% 整数档，合计 100，允许任一档为 0。"""
    ratio = DifficultyRatio(basic=0, understanding=40, deep_question=60)
    assert ratio.basic == 0 and ratio.understanding == 40 and ratio.deep_question == 60
    assert DifficultyRatio(basic=100, understanding=0, deep_question=0).basic == 100


def test_difficulty_ratio_rejects_non_ten_step() -> None:
    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=35, understanding=40, deep_question=25)


def test_difficulty_ratio_rejects_out_of_range() -> None:
    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=110, understanding=-10, deep_question=0)
    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=40, understanding=40, deep_question=30)


def test_difficulty_ratio_rejects_all_zero() -> None:
    """比例全 0 为非法配置（契约 3.5/4.1：INVALID_PREFERENCES 语义）。"""
    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=0, understanding=0, deep_question=0)


def test_generation_config_requires_coverage_mode() -> None:
    """V2.5：quantity_tendency 改名 coverage_mode（COMPACT/BALANCED/EXTENSIVE）。"""
    config = GenerationConfig(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )
    assert config.coverage_mode == "BALANCED"
    with pytest.raises(pydantic.ValidationError):
        GenerationConfig(
            coverage_mode="HUGE",
            difficulty_ratio=DifficultyRatio(basic=100, understanding=0, deep_question=0),
        )
    # V2.5 改名后旧字段不可访问（字段不存在，访问即 AttributeError）
    assert not hasattr(config, "quantity_tendency")
