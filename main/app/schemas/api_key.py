"""API Key schema（openapi ApiKey；structure-contract 3.1/6.2）。"""

from pydantic import BaseModel, Field


class ApiKeyPutRequest(BaseModel):
    api_key: str = Field(min_length=1)


class ApiKey(BaseModel):
    status: str  # AVAILABLE/INVALID/INSUFFICIENT_BALANCE/UNKNOWN
    masked_key: str
    # 无默认值必填（openapi required + 守卫 is_required）；UNKNOWN 时 handler 显式传 None
    updated_at: str | None
