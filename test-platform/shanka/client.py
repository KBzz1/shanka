"""HTTP 抽象:设备头/幂等键/429 重试/请求节奏/脱敏日志/超时。

每次请求后自动经 shanka.logging 记录请求事件(request_id 取后端 X-Request-ID);
PUT /api-key 路径不记录事件(凭据脱敏,红线 4)。
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
        device_id: str | None = None,
        pace: float = _PACE_DEFAULT,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id or str(uuid.uuid4())
        self.pace = pace
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        idempotent: bool = False,
        step: str = "",
    ) -> Response:
        started = time.monotonic()
        headers = {
            "X-Device-ID": self.device_id,
            "Content-Type": "application/json",
        }
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        data = json.dumps(body).encode() if body is not None else None

        status, payload, resp_headers = 0, None, {}
        for attempt in range(_MAX_RETRY + 1):
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
                if e.code == 429 and attempt < _MAX_RETRY:
                    try:
                        wait = int(e.headers.get("Retry-After", "2")) + 1
                    except ValueError:  # Retry-After 非整数时按默认节奏等待
                        wait = 2
                    time.sleep(wait)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < _MAX_RETRY:
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
        # 脱敏:PUT /api-key 不记录请求事件(请求体与响应含凭据相关信息)
        if not (method == "PUT" and path == "/api-key"):
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
