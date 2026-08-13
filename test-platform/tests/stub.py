"""场景逻辑层测试共用:StubClient(无网络,录制调用序列)+ 响应脚本工具。"""

from typing import Any, Callable

from shanka.client import Response

Handler = Callable[[str, str, dict | None], Response]


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

    def set_token(self, token: str) -> None:
        self._token = token
        self.calls.append(("set_token", token, None))

    def register(self, username: str, password: str) -> Response:
        self.calls.append(("register", username, None))
        return self._handler("register", "/auth/register", {"username": username, "password": password})

    def login(self, username: str, password: str) -> Response:
        self.calls.append(("login", username, None))
        return self._handler("login", "/auth/login", {"username": username, "password": password})

    def logout(self) -> Response:
        self.calls.append(("logout", "", None))
        return self._handler("logout", "/auth/logout", None)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotent: bool = False,
        retry: bool = True,
        step: str = "",
    ) -> Response:
        self.calls.append((method, path, body))
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
