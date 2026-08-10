"""写操作 raw body 捕获中间件（幂等 body 比对载体，F1 幂等原语消费）。

仅对写方法（POST/PUT/PATCH/DELETE）读取 body 缓存到 request.state.raw_body（bytes）；
GET/HEAD 不读取。请求日志不记录 body（红线 4），本中间件只缓存不落日志。
运行序：位于 Logging 内层（路由前）——Metrics → RequestID → RateLimit → DeviceID →
Logging → BodyCapture → 路由，详见 main.py 装配。
"""

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class BodyCaptureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in _WRITE_METHODS:
            body = await request.body()
            request.state.raw_body = body
        return await call_next(request)
