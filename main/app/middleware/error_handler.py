"""统一错误响应 handler（structure-contract 1.4；红线 3：错误码格式统一于 app/middleware）。

F0：AppError → 1.4 错误响应；请求日志/request_id/通用异常包装随 F1 统一中间件接入。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import AppError, http_status


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=http_status(exc.code), content=exc.to_response())
