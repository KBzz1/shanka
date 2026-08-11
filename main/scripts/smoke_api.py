"""API 连通性冒烟脚本（运行中服务的 HTTP 实链路验证）。

覆盖：探针、鉴权（X-Device-ID 必须）、牌组列表/创建/详情、幂等重放（C-04）、
错误响应结构、openapi 契约、metrics。不含任何真实密钥（生成链路需 PUT /api-key）。

用法（WSL / Windows 均可）：
    python scripts/smoke_api.py                      # 默认 http://localhost:8000
    python scripts/smoke_api.py --base-url http://127.0.0.1:8000
    python scripts/smoke_api.py --device-id <固定ID>  # 指定设备（否则每次随机）

退出码 = 失败步骤数（0 = 全部通过）。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from typing import Any

import httpx

STEPS: list[tuple[str, str]] = []

# 契约 1.6：IP 5 req/s 覆盖全部接口。主流程节奏化（默认 0.3s/请求 → 恒 ≤4 req/s），
# 否则同一 IP 连打会被限流 429（联通性测试的第一课，脚本自身不踩线）。
_PACE = 0.3


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    STEPS.append((mark, name))
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID（默认随机）")
    ap.add_argument("--pace", type=float, default=_PACE, help="请求间隔秒（契约 IP 5 req/s）")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    client = httpx.Client(base_url=base, timeout=15.0)
    device_h = {"X-Device-ID": args.device_id} if args.device_id else _device()

    def paced(method: str, path: str, **kwargs: Any) -> httpx.Response:
        time.sleep(args.pace)
        return client.request(method, path, **kwargs)

    # 1. 探针（豁免鉴权）
    r = paced("GET", "/healthz")
    check("GET /healthz -> 200", r.status_code == 200, f"({r.status_code})")
    check("healthz 响应体", r.json() == {"status": "ok"}, str(r.text[:80]))
    r = paced("GET", "/readyz")
    check("GET /readyz -> 200", r.status_code == 200, f"({r.status_code})")

    # 2. 鉴权：无 X-Device-ID 必须 401
    r = paced("GET", "/decks")
    check("GET /decks 无设备头 -> 401", r.status_code == 401, f"({r.status_code})")

    # 3. 业务链路：列表 / 创建 / 详情
    r = paced("GET", "/decks", headers=device_h)
    check("GET /decks -> 200", r.status_code == 200, f"({r.status_code})")
    items = r.json().get("items")
    check("decks 响应含 items 数组", isinstance(items, list))

    deck_name = f"smoke-{uuid.uuid4().hex[:8]}"
    payload = {"name": deck_name}
    idem = _idem()
    r = paced("POST", "/decks", json=payload, headers={**device_h, **idem})
    check("POST /decks -> 201", r.status_code == 201, f"({r.status_code})")
    deck_id = r.json().get("deck_id") if r.status_code == 201 else None
    check("创建返回 deck_id", isinstance(deck_id, str))
    check("创建返回 name", r.json().get("name") == deck_name)

    # 4. 幂等重放（C-04）：同幂等键重放 -> 原结果不 409
    r2 = paced("POST", "/decks", json=payload, headers={**device_h, **idem})
    check("幂等重放非 409", r2.status_code in (200, 201), f"({r2.status_code})")
    check("幂等重放同 deck_id", r2.json().get("deck_id") == deck_id)

    # 5. 详情核对
    if deck_id:
        r = paced("GET", f"/decks/{deck_id}", headers=device_h)
        check("GET /decks/{id} -> 200", r.status_code == 200, f"({r.status_code})")
        check("详情 deck_id 一致", r.json().get("deck_id") == deck_id)

    # 6. 错误结构：非法 body -> 400 VALIDATION_ERROR（contract 总则错误码表：结构/字段非法 400）
    r = paced("POST", "/decks", json={}, headers={**device_h, **_idem()})
    body = r.json()
    check("非法 body -> 400", r.status_code == 400, f"({r.status_code})")
    check(
        "错误结构含 error.code/localization_key",
        isinstance(body.get("error"), dict)
        and "code" in body["error"]
        and "localization_key" in body["error"],
        str(body)[:100],
    )

    # 7. 机器契约与观测（/openapi.json 需 X-Device-ID：豁免路径仅 /healthz /readyz /metrics）
    r = paced("GET", "/openapi.json", headers=device_h)
    check("GET /openapi.json -> 200", r.status_code == 200, f"({r.status_code})")
    check("openapi 含 decks 路径", "/decks" in r.json().get("paths", {}))
    r = paced("GET", "/metrics")
    check("GET /metrics -> 200", r.status_code == 200, f"({r.status_code})")

    # 8. 限流真实生效（契约 1.6：IP 5 req/s 全部接口）：连发 6 个请求 -> 至少 1 个 429 + Retry-After。
    #    预期序列含 401：RateLimit 中间件在 DeviceID 外层（裁决顺序 Metrics→RequestID→RateLimit→DeviceID），
    #    IP 窗口内的请求先过限流、再被无设备头拒（401）；窗口超限的请求直接 429。
    codes: list[int] = []
    retry_after: list[str | None] = []
    for _ in range(6):
        rr = client.get("/openapi.json")  # 不 sleep：故意超速
        codes.append(rr.status_code)
        retry_after.append(rr.headers.get("Retry-After"))
    limited = [c for c in codes if c == 429]
    check("快速连发触发 429", len(limited) >= 1, f"codes={codes}")
    check(
        "429 带 Retry-After 头",
        any(rr is not None for rr in retry_after),
        f"retry_after={retry_after}",
    )
    time.sleep(1.2)  # 越过 IP 限流 1s 窗口，确认恢复
    r = paced("GET", "/healthz")
    check("窗口过后恢复 200", r.status_code == 200, f"({r.status_code})")

    failed = sum(1 for mark, _ in STEPS if mark == "FAIL")
    print(f"\n{len(STEPS) - failed}/{len(STEPS)} 通过, {failed} 失败")
    return failed


if __name__ == "__main__":
    sys.exit(main())
