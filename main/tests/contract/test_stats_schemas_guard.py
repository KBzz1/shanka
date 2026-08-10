"""契约守卫：StatsDashboard/ReviewState/ReviewQueueItem ↔ openapi（守卫 1 扩展，红线 1）。

覆盖三个响应模型（V2-T5 裁决）：ReviewEventRequest 是请求模型、openapi 无命名 schema，跳过。
ReviewQueueItem 为 openapi allOf（Card + review_state）平铺——守卫 allOf 展平合并后比对。
ReviewState.state 用 str（非 enum）——`_is_enum(str)` False 不校验 enum 值集（与 V1 口径一致，
值域由 service 落库大写保证，openapi ReviewStateValue 枚举）。
"""

from app.schemas.review import ReviewQueueItem, ReviewState
from app.schemas.stats import StatsDashboard
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_stats_dashboard_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        StatsDashboard, openapi_schema("StatsDashboard"), load_openapi()
    )
    assert violations == []


def test_review_state_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        ReviewState, openapi_schema("ReviewState"), load_openapi()
    )
    assert violations == []


def test_review_queue_item_schema_openapi_consistent() -> None:
    """allOf 平铺（Card + review_state）：继承模型 model_fields 含父类字段 → 合并后全字段可比。"""
    violations = check_schema_consistency(
        ReviewQueueItem, openapi_schema("ReviewQueueItem"), load_openapi()
    )
    assert violations == []
