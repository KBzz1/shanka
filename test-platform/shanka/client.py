"""HTTP 抽象:Bearer 认证/幂等键/429 重试/请求节奏/脱敏日志/超时。

账号化(DESIGN 4.4/8.1):
- 普通请求在 set_token 持有 token 时携带 Authorization: Bearer <token>,未设置不带头;
  不再注入 X-Device-ID。
- register/login 不带头、不带幂等键、不自动重试(防网络重放静默创建多条会话)、
  不落请求事件(请求体含密码、响应含明文 token)。
- logout 带 Bearer 与幂等键,无论结果清空本地 token(会话已撤销/失效,不复用)。
- 每次请求后自动经 shanka.logging 记录请求事件(request_id 取后端 X-Request-ID);
  PUT /api-key 与 auth 凭据路径不记录事件(凭据脱敏,红线 4)。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from shanka import logging as shlogging

_PACE_DEFAULT = 0.3  # 契约 1.6:IP 5 req/s,节奏化避免平台自身触发限流
_MAX_RETRY = 3
# 敏感路径:请求体/响应含凭据或明文 token,不记录请求事件(红线 4)
_NO_LOG = {("PUT", "/api-key"), ("POST", "/auth/register"), ("POST", "/auth/login")}


def _parse_json(raw: str) -> Any:
    """解析 JSON 响应体;非 JSON(如网关 502 HTML 页)返回 None,不阻断调用方。"""
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


@dataclass
class Response:
    status: int
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    request_id: str | None = None
    duration_ms: int = 0


class ShankaClient:
    def __init__(
        self,
        base_url: str,
        *,
        pace: float = _PACE_DEFAULT,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.pace = pace
        self.timeout = timeout
        self._token: str | None = None

    # ---- 账号端点 ----

    def set_token(self, token: str) -> None:
        """持有 Bearer token;此后普通请求自动携带 Authorization 头(未设置不带头)。"""
        self._token = token

    def register(self, username: str, password: str) -> Response:
        """POST /auth/register:恒不带头/不重试/不落事件(凭据与响应 token 脱敏)。

        Authorization 显式剥离(即使先 set_token 也不发送)——brief 硬性语义,
        不依赖后端对 /auth/register 的鉴权豁免。
        """
        return self._credential_request("/auth/register", username, password)

    def login(self, username: str, password: str) -> Response:
        """POST /auth/login:恒不带头/不重试/不落事件。token 由调用方按需 set_token 持有。"""
        return self._credential_request("/auth/login", username, password)

    def _credential_request(self, path: str, username: str, password: str) -> Response:
        return self.request(
            "POST",
            path,
            body={"username": username, "password": password},
            retry=False,
            auth=False,
        )

    def logout(self) -> Response:
        """POST /auth/logout:Bearer 认证 + 幂等键;无论结果清空本地 token。"""
        r = self.request("POST", "/auth/logout", idempotent=True, step="auth-logout")
        self._token = None
        return r

    # ---- 普通请求 ----

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotent: bool = False,
        retry: bool = True,
        step: str = "",
        auth: bool = True,
    ) -> Response:
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        data = json.dumps(body).encode() if body is not None else None
        attempts = _MAX_RETRY + 1 if retry else 1

        status, payload, resp_headers = 0, None, {}
        for attempt in range(attempts):
            time.sleep(self.pace)
            req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode()
                    status = resp.status
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    payload = _parse_json(raw)
                    break
            except urllib.error.HTTPError as e:
                body_raw = e.read().decode()
                status = e.code
                resp_headers = {k.lower(): v for k, v in e.headers.items()}
                payload = _parse_json(body_raw)
                if e.code == 429 and attempt < attempts - 1:
                    try:
                        wait = int(e.headers.get("Retry-After", "2")) + 1
                    except ValueError:  # Retry-After 非整数时按默认节奏等待
                        wait = 2
                    time.sleep(wait)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < attempts - 1:
                    time.sleep(2)
                    continue
                break

        duration_ms = int((time.monotonic() - started) * 1000)
        response = Response(
            status=status,
            json=payload,
            headers=resp_headers,
            request_id=resp_headers.get("x-request-id"),
            duration_ms=duration_ms,
        )
        # 脱敏:PUT /api-key 与 auth 凭据路径不记录请求事件(请求体与响应含凭据相关信息)
        if (method, path) not in _NO_LOG:
            err_code = ""
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    err_code = str(err.get("code", ""))
            shlogging.event(
                "WARN" if (status == 0 or status >= 400) else "INFO",
                "request complete",
                step=step,
                method=method,
                path=path,
                status=status,
                duration_ms=duration_ms,
                request_id=response.request_id or "",
                error_code=err_code,
            )
        return response
