"""通用 schema：统一错误响应（structure-contract 1.4 / openapi `Error`）。"""

from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    localization_key: str
    actions: list[str] | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
