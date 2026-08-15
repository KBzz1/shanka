"""HTTP 抽象:Bearer 认证/幂等键/429 重试/请求节奏/脱敏日志/超时。

账号化(DESIGN 4.4/8.1):
- 普通请求在 set_token 持有 token 时携带 Authorization: Bearer <token>,未设置不带头;
  不再注入 X-Device-ID。
- register/login 不带头、不带幂等键、不自动重试(防网络重放静默创建多条会话)、
  不落请求事件(请求体含密码、响应含明文 token)。
- logout 带 Bearer 与幂等键,无论结果清空本地 token(会话已撤销/失效,不复用)。
- request 可选 idempotency_key:显式指定时复用该键(跨用户幂等复用场景),否则每次新键。
- 每次请求后自动经 shanka.logging 记录请求事件(request_id 取后端 X-Request-ID);
  PUT /api-key 与 auth 凭据路径不记录事件(凭据脱敏,红线 4)。

V2.5 复用助手(模块级函数,StubClient 同形状可共用):body/error_code 响应解析,
create_task/request_samples/start_task/abandon_task/retry_task/delete_task/submit_review/
delete_card/pending_deletion_batches/undo_deletion_batch/create_rewrite_preview/
apply_rewrite_preview/cancel_rewrite_preview——写路径统一幂等键纪律(Idempotency-Key),
路径集中于一处,场景不重复 HTTP 细节。
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

    def register(self, username: str, email: str, password: str) -> Response:
        """POST /auth/register:恒不带头/不重试/不落事件(凭据与响应 token 脱敏)。

        Authorization 显式剥离(即使先 set_token 也不发送)——brief 硬性语义,
        不依赖后端对 /auth/register 的鉴权豁免。
        """
        return self._credential_request(
            "/auth/register", {"username": username, "email": email, "password": password}
        )

    def login(self, email: str, password: str) -> Response:
        """POST /auth/login:恒不带头/不重试/不落事件。token 由调用方按需 set_token 持有。"""
        return self._credential_request("/auth/login", {"email": email, "password": password})

    def _credential_request(self, path: str, body: dict) -> Response:
        return self.request("POST", path, body=body, retry=False, auth=False)

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
        idempotency_key: str | None = None,
        retry: bool = True,
        step: str = "",
        auth: bool = True,
    ) -> Response:
        started = time.monotonic()
        headers = {"Content-Type": "application/json"}
        if auth and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if idempotent:
            # idempotency_key 显式指定时复用该键(跨用户幂等复用场景);缺省每次新键
            headers["Idempotency-Key"] = (
                idempotency_key if idempotency_key is not None else str(uuid.uuid4())
            )
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


# ---- 响应解析(V2.5 场景复用;StubClient 同形状) ----


def body(r: Response) -> dict:
    """安全取响应 JSON 字典(非 dict/解析失败返回空字典)。"""
    return r.json if isinstance(r.json, dict) else {}


def error_code(r: Response) -> str:
    """统一错误响应的稳定错误码(契约 1.4;非错误响应返回空串)。"""
    err = body(r).get("error")
    return err.get("code", "") if isinstance(err, dict) else ""


# ---- V2.5 复用请求助手(写路径统一幂等键纪律;GET 简单路径由场景直连 request) ----


def create_task(
    c: ShankaClient,
    *,
    project_id: str,
    deck_id: str,
    chapter_ids: list[str],
    generation_config: dict,
    step: str = "task-create",
) -> Response:
    """POST /projects/{project_id}/tasks:建立 DRAFT 自动保存任务(幂等)。"""
    return c.request(
        "POST", f"/projects/{project_id}/tasks",
        body={"deck_id": deck_id, "chapter_ids": chapter_ids,
              "generation_config": generation_config},
        idempotent=True, step=step,
    )


def request_samples(c: ShankaClient, task_id: str, step: str = "task-samples") -> Response:
    """POST /tasks/{task_id}/samples:持久化样卡请求(幂等;worker 后台完成)。"""
    return c.request("POST", f"/tasks/{task_id}/samples", idempotent=True, step=step)


def start_task(c: ShankaClient, task_id: str, step: str = "task-start") -> Response:
    """POST /tasks/{task_id}/start:校验样卡 hash 后进入 GENERATING(幂等)。"""
    return c.request("POST", f"/tasks/{task_id}/start", idempotent=True, step=step)


def abandon_task(c: ShankaClient, task_id: str, step: str = "task-abandon") -> Response:
    """POST /tasks/{task_id}/abandon:放弃(仅正式生成前状态,幂等)。"""
    return c.request("POST", f"/tasks/{task_id}/abandon", idempotent=True, step=step)


def retry_task(c: ShankaClient, task_id: str, step: str = "task-retry") -> Response:
    """POST /tasks/{task_id}/retry:失败任务创建关联新任务(幂等;新任务可沿用已确认样卡)。"""
    return c.request("POST", f"/tasks/{task_id}/retry", idempotent=True, step=step)


def delete_task(
    c: ShankaClient,
    task_id: str,
    *,
    delete_generated_cards: bool = False,
    step: str = "task-delete",
) -> Response:
    """DELETE /tasks/{task_id}:删除终态任务(幂等)。"""
    q = "?delete_generated_cards=true" if delete_generated_cards else ""
    return c.request("DELETE", f"/tasks/{task_id}{q}", idempotent=True, step=step)


def submit_review(
    c: ShankaClient,
    *,
    card_id: str,
    rating: str,
    client_event_id: str,
    idempotency_key: str | None = None,
    step: str = "review-event",
) -> Response:
    """POST /review-events:提交评级(幂等;显式键重放返回首次结果,不重复计数)。"""
    return c.request(
        "POST", "/review-events",
        body={"card_id": card_id, "rating": rating, "client_event_id": client_event_id},
        idempotent=True, idempotency_key=idempotency_key, step=step,
    )


def delete_card(c: ShankaClient, card_id: str, step: str = "card-delete") -> Response:
    """DELETE /cards/{card_id}:删除单卡进入 10 秒撤销批次(幂等;连续删除合并并重新计时)。"""
    return c.request("DELETE", f"/cards/{card_id}", idempotent=True, step=step)


def pending_deletion_batches(c: ShankaClient, step: str = "deletion-pending") -> Response:
    """GET /card-deletion-batches/pending:App 重启恢复仍有效的撤销批次。"""
    return c.request("GET", "/card-deletion-batches/pending", step=step)


def undo_deletion_batch(
    c: ShankaClient, delete_batch_id: str, step: str = "deletion-undo"
) -> Response:
    """POST /card-deletion-batches/{id}/undo:窗口内撤销整批(幂等;过期 409)。"""
    return c.request("POST", f"/card-deletion-batches/{delete_batch_id}/undo",
                     idempotent=True, step=step)


def create_rewrite_preview(
    c: ShankaClient,
    card_id: str,
    *,
    custom_requirements: str | None = None,
    step: str = "rewrite-preview",
) -> Response:
    """POST /cards/{card_id}/rewrite-previews:生成并持久化重写预览,不改原卡(幂等)。"""
    return c.request("POST", f"/cards/{card_id}/rewrite-previews",
                     body={"custom_requirements": custom_requirements},
                     idempotent=True, step=step)


def apply_rewrite_preview(
    c: ShankaClient, card_id: str, rewrite_id: str, step: str = "rewrite-apply"
) -> Response:
    """POST /cards/{card_id}/rewrite-previews/{rewrite_id}/apply:版本一致原子替换(CAS,幂等)。"""
    return c.request("POST", f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
                     idempotent=True, step=step)


def cancel_rewrite_preview(
    c: ShankaClient, card_id: str, rewrite_id: str, step: str = "rewrite-cancel"
) -> Response:
    """DELETE /cards/{card_id}/rewrite-previews/{rewrite_id}:取消预览,原卡不变(幂等)。"""
    return c.request("DELETE", f"/cards/{card_id}/rewrite-previews/{rewrite_id}",
                     idempotent=True, step=step)
