"""契约守卫：Task/KnowledgePoint/SampleRequest/TaskCreateRequest ↔ openapi（守卫 1 扩展，红线 1）。

Task 视图的 selected_chapters 为 Chapter[]（object 数组快照，契约 3.4/3.6）、
generation_config 为 $ref GenerationConfig（object）——守卫 array-of-object / object
属性递归路径（F1 支持）。KnowledgePoint 为内部资源（契约 3.6，required 七字段）——
视图模型（app/schemas/tasks.py）作为守卫锚点。status/difficulty 等用 str 注解不校验
enum 值集（既有口径），值集一致性由 structure-contract 状态机契约承载。
"""

from app.schemas.samples import SampleRequest
from app.schemas.tasks import KnowledgePoint, TaskCreateRequest
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


def test_sample_request_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        SampleRequest, openapi_schema("SampleRequest"), load_openapi()
    )
    assert violations == []


def test_task_create_request_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        TaskCreateRequest, openapi_schema("TaskCreateRequest"), load_openapi()
    )
    assert violations == []
