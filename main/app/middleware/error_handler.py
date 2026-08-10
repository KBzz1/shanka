"""统一错误响应 handler（structure-contract 1.4；红线 3：错误码格式统一于 app/middleware）。

F0：AppError → 1.4 错误响应。
F1（本扩展）：RequestValidationError → 400 VALIDATION_ERROR；Starlette HTTPException（404/405）
保持默认语义；Exception 兜底 → 500 INTERNAL_ERROR（内部细节只进日志）。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AppError, ErrorCode, http_status

logger = logging.getLogger("app.middleware.error_handler")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=http_status(exc.code), content=exc.to_response())

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 1.4 错误响应：VALIDATION_ERROR 400；message 不暴露内部细节
        err = AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
        )
        return JSONResponse(status_code=400, content=err.to_response())

    @app.exception_handler(StarletteHTTPException)
    def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 保留 FastAPI 内置 HTTPException（404/405 等）默认语义，不包装为 1.4
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled exception",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "error_code": "INTERNAL_ERROR",
            },
            exc_info=exc,
        )
        err = AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
        return JSONResponse(status_code=500, content=err.to_response())
