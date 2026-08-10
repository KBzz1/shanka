"""契约守卫：ApiKey ↔ openapi（守卫 1 扩展）。

openapi ApiKey required=[status, masked_key, updated_at]；status 是 $ref ApiKeyStatus
（string enum——str 注解不校验值集，值集一致性由 structure-contract 6.2 承载）；
updated_at 是 format: date-time string——模型 `str | None`（无默认值 → is_required()
True，required 检查通过）；UNKNOWN 时 handler 显式传 None（JSON null，裁决：守卫只校验
模型与 openapi 定义，不校验响应体 null 值）。
"""

from app.schemas.api_key import ApiKey
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_api_key_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(ApiKey, openapi_schema("ApiKey"), load_openapi())
    assert violations == []
