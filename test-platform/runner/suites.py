"""套件定义与调度入口:环境/套件/场景选择、成本闸门(--confirm-cost)、环境安全闸门(--confirm-prod)。

套件:
  quick — 0 次 LLM 调用,纯无 Key 冒烟;
  full  — 非生成场景 + api_key(合计 1 次校验调用;域场景实装后扩展,本期 = quick 成员);
  live  — full + samples/tasks/live_flow,含真实生成(LLM 合计 3,不超阈值;批量扩展后触发闸门)。
"""

from __future__ import annotations

import argparse
import logging as py_logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost, environments, logging as shlogging
from scenarios.baseline import api_smoke
from scenarios.flow import live_flow

SUITES: dict[str, list] = {
    "quick": [api_smoke],
    "full": [api_smoke],       # 域场景(identity/pdf/cards/...)实装后按 spec 场景地图扩展
    "live": [api_smoke, live_flow],
}


def llm_total(suite: str) -> int:
    return cost.aggregate(SUITES[suite])


def gate_ok(*, environment: str, confirm_prod: bool) -> bool:
    """prod 环境必须显式 --confirm-prod;local 默认放行。"""
    return not environments.is_prod(environment) or confirm_prod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="测试平台调度入口")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--suite", default="quick", choices=list(SUITES))
    ap.add_argument("--scenario", default=None, help="只跑指定场景(NAME)")
    ap.add_argument("--confirm-cost", action="store_true", help="确认 LLM 调用成本(合计 > 阈值时必需)")
    ap.add_argument("--confirm-prod", action="store_true", help="确认操作生产环境")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID(所有场景共用)")
    args = ap.parse_args(argv)

    if not gate_ok(environment=args.environment, confirm_prod=args.confirm_prod):
        print("拒绝执行:生产环境需要 --confirm-prod", file=sys.stderr)
        return 1

    scenarios = SUITES[args.suite]
    if args.scenario:
        scenarios = [s for s in scenarios if s.NAME == args.scenario]
        if not scenarios:
            print(f"场景不存在: {args.scenario}", file=sys.stderr)
            return 1

    total = cost.aggregate(scenarios)
    if cost.requires_confirm(total) and not args.confirm_cost:
        print(f"拒绝执行:LLM 调用合计 {total} 超过阈值 {cost.THRESHOLD},需 --confirm-cost", file=sys.stderr)
        return 1

    run_id = str(uuid.uuid4())
    base = environments.resolve(args.environment)
    shlogging.init_logger(run_id, Path(__file__).resolve().parents[1] / "logs" / "test-platform.log")

    failed = 0
    for mod in scenarios:
        print(f"\n===== 场景 {mod.NAME} ({mod.SUITE}) =====")
        shlogging.set_context(suite=mod.SUITE, scenario=mod.NAME, device_id=args.device_id or "")
        args_list = ["--base-url", base]
        if args.device_id:
            args_list += ["--device-id", args.device_id]
        failed += mod.main(args_list)
    print(f"\n套件 {args.suite} 完成, 失败步骤 {failed}, run_id={run_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
