"""契约守卫：Batch ↔ openapi（守卫 1 扩展，红线 1）。

Batch 视图模型（app/schemas/tasks.py）按 openapi Batch required 集合定义
（batch_id/task_id/batch_index/status/retry_count）；9 个 V5A 观测字段
（coverage_rate/duplicate_rate/三分布/difficulty_deviation/cache_hit_tokens/
cache_miss_tokens/output_tokens）与版本/model/http_status/duration/request_id/
cost_estimate 随 R-16 openapi 同步后一致——守卫事实为准修正（模型与 openapi
双向字段集合 + required + 标量/null 联合类型比对；分布 object 无 properties，
仅做字段名锚定，值类型由 structure-contract 3.7 承载）。
"""

from app.schemas.tasks import Batch as BatchView
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_batch_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(BatchView, openapi_schema("Batch"), load_openapi())
    assert violations == []
