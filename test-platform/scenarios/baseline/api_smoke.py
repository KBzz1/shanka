"""API 连通性冒烟场景(运行中服务的 HTTP 实链路验证,账号 Bearer 流程)。

覆盖:探针、鉴权(无 Bearer 401 AUTH_REQUIRED)、会话建立(register/login,env 凭据)、
牌组列表/创建/详情、幂等重放(C-04)、错误响应结构、openapi 契约、metrics、
IP 限流真实生效、结束清理与 logout。不含真实密钥(生成链路见 flow/live_flow)。
运行方式(由 runner 调度或直接):
    python3 scenarios/baseline/api_smoke.py --base-url http://localhost:8000 [--environment local|prod]
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/baseline),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/baseline/api_smoke.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, environments, logging as shlogging
from shanka.client import ShankaClient
from shanka.report import check, record, summary

NAME = "api_smoke"
SUITE = "baseline"
LLM_CALLS = 0


def _same_key_post(
    c: ShankaClient, path: str, body: dict, idem_key: str, token: str
) -> tuple[int, dict]:
    """手工同幂等键重放(C-04):client 每次自动新键会掩盖同键语义;Bearer 认证。"""
    req = urllib.request.Request(
        c.base_url + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Idempotency-Key": idem_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = None
        try:
            body = json.loads(e.read().decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None  # 网关 502/HTML 非 JSON 响应体:返回 None,由调用方守卫软 FAIL
        return e.code, body


def _burst(c: ShankaClient, path: str, n: int) -> tuple[list[int], list[str | None]]:
    """连发 n 个不 sleep 的请求,收集状态码与 Retry-After(IP 限流真实生效断言)。"""
    codes: list[int] = []
    retry_after: list[str | None] = []
    for _ in range(n):
        try:
            with urllib.request.urlopen(c.base_url + path, timeout=15) as resp:
                codes.append(resp.status)
                retry_after.append(resp.headers.get("Retry-After"))
        except urllib.error.HTTPError as e:
            codes.append(e.code)
            retry_after.append(e.headers.get("Retry-After"))
    return codes, retry_after


def run(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    password: str,
    openapi_local: str | None = None,
    same_key_post=_same_key_post,
    burst=_burst,
) -> int:
    """核心流程(同键重放/突发请求可注入,供无网络逻辑层测试)。"""
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 1. 探针(豁免鉴权)
    r = c.request("GET", "/healthz", step="healthz")
    check("GET /healthz -> 200", r.status == 200, f"({r.status})")
    check("healthz 响应体", r.json == {"status": "ok"}, str(r.json)[:80])
    r = c.request("GET", "/readyz", step="readyz")
    check("GET /readyz -> 200", r.status == 200, f"({r.status})")

    # 2. 鉴权:无 Bearer 必须 401(客户端未持有 token 不带头)
    r = c.request("GET", "/decks", step="no-token-decks")
    check("GET /decks 无 Bearer -> 401", r.status == 401, f"({r.status})")
    err = (r.json or {}).get("error") if isinstance(r.json, dict) else None
    check("401 错误码 AUTH_REQUIRED",
          isinstance(err, dict) and err.get("code") == "AUTH_REQUIRED", str(err)[:80])

    # 3. 会话建立:local register(已存在回落 login),prod 只 login
    session = account.bootstrap(c, environment=environment, username=username, password=password)
    check("建立会话(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0

    # 4. 业务链路:列表 / 创建 / 详情(全 Bearer)
    r = c.request("GET", "/decks", step="deck-list")
    check("GET /decks -> 200", r.status == 200, f"({r.status})")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    check("decks 响应含 items 数组", isinstance(items, list))

    deck_name = f"smoke-{time.time_ns() % 10**8}"
    r = c.request("POST", "/decks", body={"name": deck_name}, idempotent=True, step="deck-create")
    check("POST /decks -> 201", r.status == 201, f"({r.status})")
    deck_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
    check("创建返回 deck_id", isinstance(deck_id, str))
    created_name = r.json.get("name") if isinstance(r.json, dict) else None
    check("创建返回 name", created_name == deck_name, str(created_name)[:80])

    # 5. 幂等重放(C-04):同幂等键重放 -> 原结果不 409
    key = str(uuid.uuid4())
    time.sleep(c.pace)
    st1, body1 = same_key_post(c, "/decks", {"name": deck_name}, key, session["access_token"])
    time.sleep(c.pace)
    st2, body2 = same_key_post(c, "/decks", {"name": deck_name}, key, session["access_token"])
    check("幂等重放非 409", st2 in (200, 201), f"({st2})")
    replay1 = body1.get("deck_id") if isinstance(body1, dict) else None
    replay2 = body2.get("deck_id") if isinstance(body2, dict) else None
    check("幂等重放同 deck_id",
          isinstance(replay1, str) and isinstance(replay2, str) and replay1 == replay2,
          f"{replay1} vs {replay2}"[:80])
    # 创建与重放各自落库的 deck_id 全量登记(重放键与创建键不同,按 C-04 语义另建牌组)
    created_ids = {i for i in (deck_id, replay1, replay2) if isinstance(i, str)}

    # 6. 详情核对
    if deck_id:
        r = c.request("GET", f"/decks/{deck_id}", step="deck-detail")
        check("GET /decks/{id} -> 200", r.status == 200, f"({r.status})")
        detail_deck_id = r.json.get("deck_id") if isinstance(r.json, dict) else None
        check("详情 deck_id 一致", detail_deck_id == deck_id, str(detail_deck_id)[:80])

    # 7. 错误结构:非法 body -> 400 VALIDATION_ERROR
    r = c.request("POST", "/decks", body={}, idempotent=True, step="deck-invalid")
    body = r.json or {}
    check("非法 body -> 400", r.status == 400, f"({r.status})")
    err = body.get("error")
    check("错误结构含 error.code/localization_key",
          isinstance(err, dict) and "code" in err and "localization_key" in err, str(body)[:100])

    # 8. 机器契约与观测
    if openapi_local:
        with open(openapi_local, encoding="utf-8") as f:
            spec = json.load(f)
        check("本地 openapi 含 decks 路径", "/decks" in spec.get("paths", {}))
    else:
        r = c.request("GET", "/openapi.json", step="openapi")
        check("GET /openapi.json -> 200", r.status == 200, f"({r.status})")
        paths = r.json.get("paths", {}) if isinstance(r.json, dict) else {}
        check("openapi 含 decks 路径", "/decks" in paths)
    r = c.request("GET", "/metrics", step="metrics")
    check("GET /metrics -> 200", r.status == 200, f"({r.status})")

    # 9. 限流真实生效:连发 6 个(不 sleep)-> 至少 1 个 429 + Retry-After
    codes, retry_after = burst(c, "/openapi.json", 6)
    check("快速连发触发 429", codes.count(429) >= 1, f"codes={codes}")
    check("429 带 Retry-After 头", any(v is not None for v in retry_after), f"{retry_after}")
    time.sleep(1.2)  # 越过 IP 限流 1s 窗口
    r = c.request("GET", "/healthz", step="healthz-after")
    check("窗口过后恢复 200", r.status == 200, f"({r.status})")

    # 10. 清理创建的 smoke-* 牌组并注销会话(清理失败仅 WARN,不影响退出码)
    for did in sorted(created_ids):
        r = c.request("DELETE", f"/decks/{did}", idempotent=True, step="deck-cleanup")
        if r.status not in (200, 204):
            shlogging.event("WARN", "清理 smoke 牌组失败", deck_id=did, status=r.status)
    r = c.logout()
    check("logout -> 204", r.status == 204, f"({r.status})")

    if created:
        record("local_test_users_created", created)
    return summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--run-id", default=None, help="runner 注入;直跑时自动生成")
    ap.add_argument("--pace", type=float, default=0.3, help="请求间隔秒(契约 IP 5 req/s)")
    ap.add_argument("--openapi-local", default=None, help="本地 openapi 文件路径(默认运行时 /openapi.json)")
    args = ap.parse_args(argv)
    try:
        username, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1
    c = ShankaClient(args.base_url, pace=args.pace)
    return run(c, environment=args.environment, username=username, password=password,
               openapi_local=args.openapi_local)


if __name__ == "__main__":
    sys.exit(main())
