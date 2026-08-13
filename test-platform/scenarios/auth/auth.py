"""账号认证最小链路场景:register/login/me/logout 与 401 语义。

覆盖:无 Bearer 业务请求 401 AUTH_REQUIRED、错误密码 401 INVALID_CREDENTIALS、
会话建立(local 先 register、账号已存在回落 login;prod 只 login,禁自动注册)、
me 用户自述、logout 撤销会话(204,随后 me 401)。
凭据读自 env(SHANKA_TEST_USERNAME/SHANKA_TEST_PASSWORD),不经 CLI 参数;无 LLM 调用。
运行方式(由 runner 调度或直接):
    python3 scenarios/auth/auth.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID]
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/auth),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/auth/auth.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, environments, logging as shlogging
from shanka.client import Response, ShankaClient
from shanka.report import check, record, summary

NAME = "auth"
SUITE = "auth"
LLM_CALLS = 0


def _error_code(r: Response) -> str:
    body = r.json if isinstance(r.json, dict) else {}
    err = body.get("error")
    return err.get("code", "") if isinstance(err, dict) else ""


def run(c: ShankaClient, *, environment: str, username: str, password: str) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 1. 未认证:业务接口拒绝(统一 AUTH_REQUIRED,不区分缺失/无效)
    r = c.request("GET", "/decks", step="no-token-decks")
    check("无 Bearer GET /decks -> 401", r.status == 401, f"({r.status})")
    check("401 错误码 AUTH_REQUIRED", _error_code(r) == "AUTH_REQUIRED", _error_code(r))

    # 2. 错误密码:统一 401 INVALID_CREDENTIALS(不区分用户名不存在;等长错误密码避免长度校验差异)
    r = c.login(username, account.wrong_password(password))
    check("错误密码 login -> 401", r.status == 401, f"({r.status})")
    check("401 错误码 INVALID_CREDENTIALS", _error_code(r) == "INVALID_CREDENTIALS", _error_code(r))

    # 3. 会话建立:local register(已存在回落 login),prod 只 login
    session = account.bootstrap(c, environment=environment, username=username, password=password)
    check("建立会话(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0

    # 4. me 自述:用户名与 user_id 与登录一致
    r = c.request("GET", "/auth/me", step="me")
    check("GET /auth/me -> 200", r.status == 200, f"({r.status})")
    me_user = (r.json or {}).get("user") if isinstance(r.json, dict) else {}
    check("me 用户名为当前账号", me_user.get("username") == username, str(me_user)[:80])
    check("me user_id 与登录一致", me_user.get("user_id") == session["user_id"], str(me_user)[:80])

    # 5. logout 撤销会话:204,随后 me 401
    r = c.logout()
    check("POST /auth/logout -> 204", r.status == 204, f"({r.status})")
    r = c.request("GET", "/auth/me", step="me-after-logout")
    check("logout 后 me -> 401", r.status == 401, f"({r.status})")

    if created:
        record("local_test_users_created", created)
    return summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--run-id", default=None, help="runner 注入;直跑时自动生成")
    ap.add_argument("--pace", type=float, default=0.3, help="请求间隔秒(契约 IP 5 req/s)")
    args = ap.parse_args(argv)
    try:
        username, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1
    c = ShankaClient(args.base_url, pace=args.pace)
    return run(c, environment=args.environment, username=username, password=password)


if __name__ == "__main__":
    sys.exit(main())
