"""场景逻辑层测试共用:StubClient(无网络,录制调用序列)+ 响应脚本工具。

按 Authorization 头/幂等键分派的最小扩展(毛刺 #4 跨用户幂等测试):handler 可声明
4 参(method, path, body, auth)或 5 参(再 + idempotency_key),3 参旧签名不变——
auth 为当前 token(set_token 设置),idempotency_key 为显式幂等键
(idempotent=True 且未显式指定时传 None)。
"""

import inspect
from typing import Any, Callable

from shanka.client import Response

Handler = Callable[..., Response]


class StubClient:
    """ShankaClient 形状的最小替身:录制调用序列,响应由 handler 决定(无网络)。"""

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        base_url: str = "http://stub",
        pace: float = 0.0,
    ) -> None:
        self.base_url = base_url
        self.pace = pace
        self.calls: list[tuple[str, str, dict | None]] = []
        self._token: str | None = None
        self._handler: Handler = handler or (lambda m, p, b: Response(200, {"status": "ok"}))
        # handler 形参个数决定分派形态:>=5 传 auth+key,4 传 auth,否则 3 参旧签名
        self._handler_arity = self._arity_of(self._handler)

    @staticmethod
    def _arity_of(handler: Handler) -> int:
        params = list(inspect.signature(handler).parameters.values())
        if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
            return 5  # *args 可接收全量参数
        return min(max(len(params), 3), 5)

    def set_token(self, token: str) -> None:
        self._token = token
        self.calls.append(("set_token", token, None))

    def register(self, username: str, email: str, password: str) -> Response:
        self.calls.append(("register", username, None))
        return self._dispatch(
            "register", "/auth/register",
            {"username": username, "email": email, "password": password}, None
        )

    def login(self, email: str, password: str) -> Response:
        self.calls.append(("login", email, None))
        return self._dispatch(
            "login", "/auth/login", {"email": email, "password": password}, None
        )

    def logout(self) -> Response:
        self.calls.append(("logout", "", None))
        return self._dispatch("logout", "/auth/logout", None, None)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
        retry: bool = True,
        step: str = "",
    ) -> Response:
        self.calls.append((method, path, body))
        # 与 ShankaClient 同参:显式 idempotency_key 复用该键,否则不向 handler 传键
        key = idempotency_key if idempotent else None
        return self._dispatch(method, path, body, key)

    def _dispatch(
        self, method: str, path: str, body: dict | None, idempotency_key: str | None
    ) -> Response:
        if self._handler_arity >= 5:
            return self._handler(method, path, body, self._token, idempotency_key)
        if self._handler_arity == 4:
            return self._handler(method, path, body, self._token)
        return self._handler(method, path, body)


def script(*routes: tuple[str, Response]) -> Handler:
    """按 path 返回预设 Response(未命中返回 200 ok)。"""
    table = {path: resp for path, resp in routes}

    def handler(method: str, path: str, body: dict | None) -> Response:
        return table.get(path, Response(200, {"status": "ok"}))

    return handler


def session_body(user_id: str = "u-primary", username: str = "tester") -> dict[str, Any]:
    """register/login 成功响应形状(DESIGN 4.4)。"""
    return {
        "user": {"user_id": user_id, "username": username, "created_at": "2026-08-14T00:00:00Z"},
        "access_token": f"tok-{user_id}",
        "token_type": "Bearer",
        "expires_at": "2026-09-13T00:00:00Z",
    }
