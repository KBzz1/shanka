"""用户隔离场景:两用户资源互不可见(跨用户统一 404),observability 按 user 隔离。

local: 主账号(env 凭据,register 或 login)创建牌组;临时账号(run_id 命名,本地注册)
跨用户访问主账号牌组 -> 404、列表不可见、写操作 404、quality-summary 为空;
跨用户幂等键复用:两用户同 Idempotency-Key 同 body 各自成功、互不重放,
同用户重放同 key 得原响应不新建;
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


def _cleanup_decks(c: ShankaClient, prefix: str, *, note: str = "异常路径残留牌组已清理") -> None:
    """按名称前缀清理本 run 的 iso-* 牌组(尽力而为,失败仅 WARN);note 供正常路径定制措辞。"""
    r = c.request("GET", "/decks", step="cleanup-deck-list")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    if not isinstance(items, list):
        print(f"    [warn] 异常路径牌组清理:列表失败(HTTP {r.status}),请人工核对 {prefix}* 牌组")
        return
    for it in items:
        if not isinstance(it, dict) or not str(it.get("name", "")).startswith(prefix):
            continue
        deck_id = it.get("deck_id")
        if not isinstance(deck_id, str):
            continue
        c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="cleanup-deck-delete")
        print(f"    [warn] {note}: {it.get('name')}")


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


def run(c: ShankaClient, *, environment: str, username: str, email: str,
        password: str, run_id: str) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 主账号会话(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username,
                                email=email, password=password)
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
        # 异常路径:POST 201 但无 deck_id——牌组可能已建,按前缀兜底清理再注销(不留 iso-* 残留)
        _cleanup_decks(c, f"iso-{run_id[:8]}")
        _logout(c)
        if created:
            record("local_test_users_created", created)
        return summary()

    # 临时账号(run_id 命名,本地注册;user 行残留按 run_id 计数报告)
    second_name = account.temp_username(run_id, "iso")
    second = account.bootstrap(c, environment=environment, username=second_name,
                               email=account.temp_email(run_id, "iso"),
                               password=account.temp_password())
    check("临时账号注册", second is not None, f"user={second_name}")
    if second is None:
        # 异常路径:bootstrap 失败时本地 token 状态不承诺——切回主账号确定性清理;
        # 临时账号 session 若已建而 client 未持有 token 则无法撤销,仅 WARN 登记
        c.set_token(session["access_token"])
        print(f"    [warn] 临时账号 {second_name} 会话可能未撤销(注册失败路径,无 token 可注销)")
        _cleanup_decks(c, f"iso-{run_id[:8]}")
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

    # 跨用户幂等键复用(毛刺 #4,DESIGN 8.2 缺口):不同用户同 Idempotency-Key 同 body
    # 各自成功、互不重放;同用户重放同 key 得原响应不新建。
    idem_key = str(uuid.uuid4())
    idem_body = {"name": f"iso-{run_id[:8]}-idem"}
    r = c.request("POST", "/decks", body=idem_body, idempotent=True,
                  idempotency_key=idem_key, step="idem-create-second")
    check("跨用户幂等:临时账号 POST -> 201", r.status == 201, f"({r.status})")
    second_idem_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
    check("跨用户幂等:临时账号返回 deck_id", isinstance(second_idem_id, str))
    c.set_token(session["access_token"])
    r = c.request("POST", "/decks", body=idem_body, idempotent=True,
                  idempotency_key=idem_key, step="idem-create-main")
    check("跨用户幂等:主账号 POST -> 201", r.status == 201, f"({r.status})")
    main_idem_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
    check("跨用户幂等:主账号返回 deck_id", isinstance(main_idem_id, str))
    if isinstance(second_idem_id, str) and isinstance(main_idem_id, str):
        check("跨用户幂等:两用户 deck_id 不同", second_idem_id != main_idem_id,
              f"second={second_idem_id} main={main_idem_id}")
    # 主账号重放:同 key 同 body -> 原响应不新建(重放语义保持)
    r = c.request("POST", "/decks", body=idem_body, idempotent=True,
                  idempotency_key=idem_key, step="idem-replay-main")
    replay_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
    check("跨用户幂等:主账号重放 -> 原 deck_id 不新建",
          r.status in (200, 201) and isinstance(replay_id, str) and replay_id == main_idem_id,
          f"({r.status}, {replay_id})")
    r = c.request("GET", "/decks", step="idem-deck-list")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    if isinstance(items, list):
        check("跨用户幂等:主账号同名牌组仅一张",
              sum(1 for it in items
                  if isinstance(it, dict) and it.get("name") == idem_body["name"]) == 1)
    # 清理:两账号 idem 牌组各自前缀清理(切回临时 token,保持后续注销对象正确)
    _cleanup_decks(c, f"iso-{run_id[:8]}-idem", note="跨用户幂等牌组已清理")
    c.set_token(second["access_token"])
    _cleanup_decks(c, f"iso-{run_id[:8]}-idem", note="跨用户幂等牌组已清理")

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
        username, email, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1
    c = ShankaClient(args.base_url, pace=args.pace)
    return run(c, environment=args.environment, username=username, email=email,
               password=password, run_id=args.run_id or str(uuid.uuid4()))


if __name__ == "__main__":
    sys.exit(main())
