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
        # 2026-08-11 联调诊断：错误码写入 request.state，请求日志可记录（此前仅 status）。
        request.state.error_code = exc.code
        response = JSONResponse(status_code=http_status(exc.code), content=exc.to_response())
        # D-05/契约 1.4：受保护接口 401 一律携带 WWW-Authenticate: Bearer——auth 中间件
        # 已对自身短路路径加头，此处统一覆盖 handler 层抛出的 AUTH_REQUIRED/AUTH_INVALID
        # （如 me 窄竞态：token 经 resolve 后被撤销，service 抛 AUTH_INVALID 的 401 路径）。
        # 重复设置同一值无害（handler 路径不经过 auth 中间件的加头响应）。
        # INVALID_CREDENTIALS（login 失败）按 DESIGN §4.3 不加。
        if exc.code in (ErrorCode.AUTH_REQUIRED, ErrorCode.AUTH_INVALID):
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 1.4 错误响应：VALIDATION_ERROR 400；message 不暴露内部细节
        request.state.error_code = ErrorCode.VALIDATION_ERROR
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
        response = JSONResponse(status_code=500, content=err.to_response())
        # final review I-1：未处理异常 500 由最外层 ServerErrorMiddleware 兜底直发
        # （exception handler 按 FastAPI 装配成为其 handler，绕过全部用户中间件），
        # RequestIDMiddleware 的 call_next 因异常传播不会返回、响应头写入不执行；
        # 此处补 X-Request-ID，保证 1.4 契约的 request_id 日志关联对 500 同样成立。
        response.headers["X-Request-ID"] = getattr(request.state, "request_id", "")
        return response
