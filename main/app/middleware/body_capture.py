"""写操作 raw body 捕获中间件（幂等 body 比对载体，F1 幂等原语消费）。

仅对写方法（POST/PUT/PATCH/DELETE）读取 body 缓存到 request.state.raw_body（bytes）；
GET/HEAD 不读取。请求日志不记录 body（红线 4），本中间件只缓存不落日志。
运行序：位于 Logging 内层（路由前）——Metrics → RequestID → IpRateLimit → Auth →
RateLimit → Logging → BodyCapture → 路由，详见 main.py 装配。

PDF 上传预检（final review I-1）：PDF 上传（V25-D-29 起为 POST
/projects/{id}/materials/pdf）且 Content-Length 头超限 → 直接 400
PDF_UPLOAD_INVALID，在读 body 之前拒绝——否则超大 body 先被全量缓存放大内存。
"""

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.errors import AppError, ErrorCode, http_status

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_PDF_UPLOAD_SUFFIX = "/materials/pdf"  # V25-D-29 起 PDF 上传入口（/pdfs 已移除）


class BodyCaptureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "POST" and request.url.path.endswith(_PDF_UPLOAD_SUFFIX):
            max_bytes = request.app.state.settings.pdf_max_size_bytes
            content_length = request.headers.get("Content-Length")
            if (
                content_length is not None
                and content_length.isdigit()
                and int(content_length) > max_bytes
            ):
                return JSONResponse(
                    status_code=http_status(ErrorCode.PDF_UPLOAD_INVALID),
                    content=AppError(
                        ErrorCode.PDF_UPLOAD_INVALID,
                        f"PDF 上传超过大小上限（{max_bytes // (1024 * 1024)}MB）",
                    ).to_response(),
                )
        if request.method in _WRITE_METHODS:
            body = await request.body()
            request.state.raw_body = body
        return await call_next(request)
