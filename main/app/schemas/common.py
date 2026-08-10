"""通用 schema：统一错误响应（structure-contract 1.4 / openapi `Error`）。"""

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    localization_key: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
