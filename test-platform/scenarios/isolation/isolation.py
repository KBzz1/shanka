"""用户隔离场景:两用户资源互不可见(跨用户统一 404),observability 按 user 隔离。

local: 主账号(env 凭据,register 或 login)创建牌组;临时账号(run_id 命名,本地注册)
跨用户访问主账号牌组 -> 404、列表不可见、写操作 404、quality-summary 为空;
结束清理牌组与两 session。临时账号 user 行不可安全删除,按 run_id 计数写入报告字段。
prod: 只读断言(禁自动注册):不创建资源,仅验证当前用户视角的 404 与列表形状。
运行方式(由 runner 调度或直接):
    python3 scenarios/isolation/isolation.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID]
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/isolation),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/isolation/isolation.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, environments, logging as shlogging
from shanka.client import ShankaClient
from shanka.report import check, record, summary

NAME = "isolation"
SUITE = "isolation"
LLM_CALLS = 0


def _logout(c: ShankaClient) -> None:
    r = c.logout()
    check("logout -> 204", r.status == 204, f"({r.status})")


def _readonly_prod(c: ShankaClient) -> int:
    """prod 只读断言:禁自动注册,不创建资源,不产生 user 行残留。"""
    r = c.request("GET", "/decks", step="deck-list")
    check("GET /decks -> 200", r.status == 200, f"({r.status})")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    check("decks 响应含 items 数组", isinstance(items, list))
    r = c.request("GET", f"/decks/{uuid.uuid4()}", step="deck-404")
    check("不存在牌组 -> 404", r.status == 404, f"({r.status})")
    r = c.request("GET", "/observability/quality-summary", step="quality-summary")
    body = r.json if isinstance(r.json, dict) else {}
    check("quality-summary -> 200 且 groups 为列表",
          r.status == 200 and isinstance(body.get("groups"), list), f"({r.status})")
    _logout(c)
    return summary()


def run(c: ShankaClient, *, environment: str, username: str, password: str, run_id: str) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 主账号会话(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username, password=password)
    check("主账号会话建立(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0

    if environments.is_prod(environment):
        return _readonly_prod(c)

    # local:两用户隔离
    deck_name = f"iso-{run_id[:8]}"
    r = c.request("POST", "/decks", body={"name": deck_name}, idempotent=True, step="deck-create")
    check("POST /decks -> 201", r.status == 201, f"({r.status})")
    deck_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
    check("创建返回 deck_id", isinstance(deck_id, str))
    if not isinstance(deck_id, str):
        _logout(c)
        if created:
            record("local_test_users_created", created)
        return summary()

    # 临时账号(run_id 命名,本地注册;user 行残留按 run_id 计数报告)
    second_name = account.temp_username(run_id, "iso")
    second = account.bootstrap(c, environment=environment, username=second_name,
                               password=account.temp_password())
    check("临时账号注册", second is not None, f"user={second_name}")
    if second is None:
        _logout(c)
        if created:
            record("local_test_users_created", created)
        return summary()
    created += 1

    # 跨用户访问:读 404、列表不可见、写 404、summary 为空
    r = c.request("GET", f"/decks/{deck_id}", step="cross-deck")
    check("跨用户 deck 详情 -> 404", r.status == 404, f"({r.status})")
    r = c.request("GET", "/decks", step="second-deck-list")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    check("第二用户列表 -> 200 含 items", r.status == 200 and isinstance(items, list), f"({r.status})")
    if isinstance(items, list):
        check("第二用户列表不含主账号牌组",
              all(it.get("deck_id") != deck_id for it in items if isinstance(it, dict)))
    r = c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="cross-deck-delete")
    check("跨用户 deck 删除 -> 404(不生效)", r.status == 404, f"({r.status})")
    r = c.request("GET", "/observability/quality-summary", step="second-quality-summary")
    body = r.json if isinstance(r.json, dict) else {}
    check("第二用户 quality-summary -> 200", r.status == 200, f"({r.status})")
    check("第二用户 summary 为空(按 user 隔离)", body.get("groups") == [], str(body)[:100])

    # 撤销临时账号会话
    _logout(c)

    # 切回主账号:清理牌组 + 注销会话
    c.set_token(session["access_token"])
    r = c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup")
    check("主账号清理牌组", r.status in (200, 204), f"({r.status})")
    _logout(c)

    if created:
        record("local_test_users_created", created)
    return summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--run-id", default=None, help="runner 注入(临时账号命名);直跑时自动生成")
    ap.add_argument("--pace", type=float, default=0.3, help="请求间隔秒(契约 IP 5 req/s)")
    args = ap.parse_args(argv)
    try:
        username, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1
    c = ShankaClient(args.base_url, pace=args.pace)
    return run(c, environment=args.environment, username=username, password=password,
               run_id=args.run_id or str(uuid.uuid4()))


if __name__ == "__main__":
    sys.exit(main())
