"""账号引导:环境感知的注册/登录(register-or-login)与本地临时测试账号命名。

凭据纪律(DESIGN 8.1):凭据只经参数传入(调用方读自 environments.credentials()),
不落日志/console/JSONL;local 先 register(409 EMAIL_TAKEN 回落 login),prod 只 login
(禁止自动注册);临时测试账号以 run_id 命名,无法安全删除的 user 行由场景按 run_id
计数写入报告字段(不新增生产账号删除接口)。
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

from shanka import environments
from shanka.client import Response, ShankaClient

_SESSION_STATUS = (200, 201)
_MAX_429_RETRIES = 3


def _retry_429(call: Callable[[], Response], what: str) -> Response:
    """429 显式重试(限 3 次):429 是服务端业务执行前的明确拒绝(限流桶检查先于
    建用户/建会话),重试无重放副作用;FR-19 防的是超时/5xx 未知结果重放,不冲突。
    """
    r = call()
    for attempt in range(_MAX_429_RETRIES):
        if r.status != 429:
            return r
        try:
            wait = int(r.headers.get("retry-after", "2")) + 1  # headers 键已小写(client.py)
        except (TypeError, ValueError):
            wait = 2
        print(f"    [429] {what} 限流,等待 {wait}s 后重试({attempt + 1}/{_MAX_429_RETRIES})")
        time.sleep(wait)
        r = call()
    return r


def auth_mode(environment: str) -> str:
    """local -> register 优先(账号已存在回落 login);prod -> 只 login(禁自动注册)。"""
    return "login" if environments.is_prod(environment) else "register"


def parse_session(r: Response) -> dict | None:
    """从 register/login 响应提取 {user_id, username, access_token};形状不符返回 None。"""
    body = r.json if isinstance(r.json, dict) else {}
    user = body.get("user")
    token = body.get("access_token")
    if not (isinstance(user, dict) and isinstance(token, str)):
        return None
    user_id = user.get("user_id")
    username = user.get("username")
    if not (isinstance(user_id, str) and isinstance(username, str)):
        return None
    return {"user_id": user_id, "username": username, "access_token": token}


def bootstrap(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    email: str,
    password: str,
) -> dict | None:
    """按环境注册或登录并 set_token;成功返回会话信息(含 created_local_user),失败返回 None。

    local: 先 register,201/200 即新建本地用户行(created_local_user=True);
           409 EMAIL_TAKEN(账号已存在)回落 login;429 按 Retry-After 重试 ≤3 次;
           仍 429 打印明确指引后返回 None;其他失败不静默回落。
    prod: 只 login,不自动注册;429 同样重试。
    """
    if auth_mode(environment) == "register":
        r = _retry_429(lambda: c.register(username, email, password), "register")
        if r.status in _SESSION_STATUS:
            session = parse_session(r)
            if session:
                c.set_token(session["access_token"])
                return {**session, "created_local_user": True}
        if r.status == 429:
            print(f"    [FAIL] register 限流重试 {_MAX_429_RETRIES} 次后仍 429,请等待限流窗口(1 小时)后重跑")
            return None
        if r.status != 409:
            return None
    r = _retry_429(lambda: c.login(email, password), "login")
    if r.status == 429:
        print(f"    [FAIL] login 限流重试 {_MAX_429_RETRIES} 次后仍 429,请等待限流窗口(1 小时)后重跑")
        return None
    if r.status not in _SESSION_STATUS:
        return None
    session = parse_session(r)
    if session is None:
        return None
    c.set_token(session["access_token"])
    return {**session, "created_local_user": False}


def temp_username(run_id: str, tag: str) -> str:
    """run_id 派生的本地临时测试账号名(3~32 位,小写字母/数字/-;同 run 内按 tag 唯一)。"""
    return f"t-{run_id.replace('-', '')[:10]}-{tag}"


def temp_email(run_id: str, tag: str) -> str:
    """run_id 派生的本地临时测试邮箱(与 temp_username 对应,不落日志)。"""
    return f"{temp_username(run_id, tag)}@local.test"


def temp_password() -> str:
    """本地临时测试账号随机密码(仅内存使用,不落日志/console)。"""
    return secrets.token_urlsafe(16)


def wrong_password(password: str) -> str:
    """等长错误密码(login 失败分支用;避免长度变化触发 400 校验差异)。"""
    tail = password[-1:]
    return password[:-1] + ("x" if tail != "x" else "y")
