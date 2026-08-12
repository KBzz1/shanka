"""API 连通性冒烟场景(运行中服务的 HTTP 实链路验证)。

覆盖:探针、鉴权(X-Device-ID 必须)、牌组列表/创建/详情、幂等重放(C-04)、
错误响应结构、openapi 契约、metrics。不含真实密钥(生成链路见 flow/live_flow)。
运行方式(由 runner 调度或直接):
    python3 scenarios/baseline/api_smoke.py --base-url http://localhost:8000
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/baseline),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/baseline/api_smoke.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka.client import ShankaClient
from shanka.report import check, summary

NAME = "api_smoke"
SUITE = "baseline"
LLM_CALLS = 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID(默认随机)")
    ap.add_argument("--pace", type=float, default=0.3, help="请求间隔秒(契约 IP 5 req/s)")
    ap.add_argument("--openapi-local", default=None, help="本地 openapi 文件路径(默认运行时 /openapi.json)")
    args = ap.parse_args(argv)

    c = ShankaClient(args.base_url, device_id=args.device_id, pace=args.pace)

    # 1. 探针(豁免鉴权)
    r = c.request("GET", "/healthz", step="healthz")
    check("GET /healthz -> 200", r.status == 200, f"({r.status})")
    check("healthz 响应体", r.json == {"status": "ok"}, str(r.json)[:80])
    r = c.request("GET", "/readyz", step="readyz")
    check("GET /readyz -> 200", r.status == 200, f"({r.status})")

    # 2. 鉴权:无 X-Device-ID 必须 401(不经 client 的设备头,直连)
    try:
        with urllib.request.urlopen(c.base_url + "/decks", timeout=15) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        e.close()
    check("GET /decks 无设备头 -> 401", status == 401, f"({status})")

    # 3. 业务链路:列表 / 创建 / 详情
    r = c.request("GET", "/decks", step="deck-list")
    check("GET /decks -> 200", r.status == 200, f"({r.status})")
    items = r.json.get("items") if isinstance(r.json, dict) else None
    check("decks 响应含 items 数组", isinstance(items, list))

    deck_name = f"smoke-{time.time_ns() % 10**8}"
    r = c.request("POST", "/decks", body={"name": deck_name}, idempotent=True, step="deck-create")
    check("POST /decks -> 201", r.status == 201, f"({r.status})")
    deck_id = r.json.get("deck_id") if r.status == 201 else None
    check("创建返回 deck_id", isinstance(deck_id, str))
    check("创建返回 name", r.json.get("name") == deck_name)

    # 4. 幂等重放(C-04):同幂等键重放 -> 原结果不 409
    #    (client 每次自动新键会掩盖同键语义,此处手工构造同键两次 POST)
    import uuid as _uuid
    key = str(_uuid.uuid4())
    def _post_with_key(path: str, body: dict, idem_key: str):
        import json as _json
        import urllib.request as _ur
        req = _ur.Request(
            c.base_url + path,
            data=_json.dumps(body).encode(),
            headers={"X-Device-ID": c.device_id, "Content-Type": "application/json",
                     "Idempotency-Key": idem_key},
            method="POST",
        )
        try:
            with _ur.urlopen(req, timeout=15) as resp:
                return resp.status, _json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, _json.loads(e.read().decode() or "{}")

    time.sleep(c.pace)
    st1, body1 = _post_with_key("/decks", {"name": deck_name}, key)
    time.sleep(c.pace)
    st2, body2 = _post_with_key("/decks", {"name": deck_name}, key)
    check("幂等重放非 409", st2 in (200, 201), f"({st2})")
    check("幂等重放同 deck_id", body2.get("deck_id") == body1.get("deck_id"))

    # 5. 详情核对
    if deck_id:
        r = c.request("GET", f"/decks/{deck_id}", step="deck-detail")
        check("GET /decks/{id} -> 200", r.status == 200, f"({r.status})")
        check("详情 deck_id 一致", r.json.get("deck_id") == deck_id)

    # 6. 错误结构:非法 body -> 400 VALIDATION_ERROR
    r = c.request("POST", "/decks", body={}, idempotent=True, step="deck-invalid")
    body = r.json or {}
    check("非法 body -> 400", r.status == 400, f"({r.status})")
    err = body.get("error")
    check("错误结构含 error.code/localization_key",
          isinstance(err, dict) and "code" in err and "localization_key" in err,
          str(body)[:100])

    # 7. 机器契约与观测
    if args.openapi_local:
        import json as _json
        with open(args.openapi_local, encoding="utf-8") as f:
            spec = _json.load(f)
        check("本地 openapi 含 decks 路径", "/decks" in spec.get("paths", {}))
    else:
        r = c.request("GET", "/openapi.json", step="openapi")
        check("GET /openapi.json -> 200", r.status == 200, f"({r.status})")
        paths = r.json.get("paths", {}) if isinstance(r.json, dict) else {}
        check("openapi 含 decks 路径", "/decks" in paths)
    r = c.request("GET", "/metrics", step="metrics")
    check("GET /metrics -> 200", r.status == 200, f"({r.status})")

    # 8. 限流真实生效:连发 6 个(不 sleep)-> 至少 1 个 429 + Retry-After
    codes: list[int] = []
    retry_after: list[str | None] = []
    for _ in range(6):
        try:
            with urllib.request.urlopen(c.base_url + "/openapi.json", timeout=15) as resp:
                codes.append(resp.status)
                retry_after.append(resp.headers.get("Retry-After"))
        except urllib.error.HTTPError as e:
            codes.append(e.code)
            retry_after.append(e.headers.get("Retry-After"))
    check("快速连发触发 429", codes.count(429) >= 1, f"codes={codes}")
    check("429 带 Retry-After 头", any(v is not None for v in retry_after), f"{retry_after}")
    time.sleep(1.2)  # 越过 IP 限流 1s 窗口
    r = c.request("GET", "/healthz", step="healthz-after")
    check("窗口过后恢复 200", r.status == 200, f"({r.status})")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
