"""套件定义与调度入口:环境/套件/场景选择、成本闸门(--confirm-cost)、环境安全闸门(--confirm-prod)。

套件:
  quick — auth + api_smoke,0 次 LLM 调用,纯无 Key 冒烟;
  full  — 非生成场景(auth/isolation/api_smoke,0 次 LLM;域场景实装后扩展);
  live  — full + live_flow,含真实生成(最坏调用预算由 fixture 推导,超阈值必须 --confirm-cost);
  v25   — v25_core_flow + v25_recovery(V2.5 非可视化 Release 主链路:cost-confirmed 生成套件
          [推导预算 + 重写预览] + zero-LLM 恢复套件)。
凭据只从环境变量读取(SHANKA_TEST_USERNAME/SHANKA_TEST_EMAIL/SHANKA_TEST_PASSWORD),
缺失拒绝执行;run_id 由 runner 生成并注入场景,日志身份字段为 user_id(会话建立前为空)。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost, environments, logging as shlogging
from scenarios.auth import auth
from scenarios.baseline import api_smoke
from scenarios.flow import live_flow, v25_core_flow, v25_recovery
from scenarios.isolation import isolation

SUITES: dict[str, list] = {
    "quick": [auth, api_smoke],
    "full": [auth, isolation, api_smoke],
    "live": [auth, isolation, api_smoke, live_flow],
    "v25": [v25_core_flow, v25_recovery],
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
    ap.add_argument("--confirm-cost", action="store_true", help="确认 LLM 最坏调用预算(合计 > 阈值时必需)")
    ap.add_argument("--confirm-prod", action="store_true", help="确认操作生产环境(禁止自动注册)")
    args = ap.parse_args(argv)

    if not gate_ok(environment=args.environment, confirm_prod=args.confirm_prod):
        print("拒绝执行:生产环境需要 --confirm-prod", file=sys.stderr)
        return 1

    # 凭据只从环境变量读取(不进 CLI/console/JSONL);缺失报错退出,不自动注册
    try:
        environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1

    scenarios = SUITES[args.suite]
    if args.scenario:
        scenarios = [s for s in scenarios if s.NAME == args.scenario]
        if not scenarios:
            print(f"场景不存在: {args.scenario}", file=sys.stderr)
            return 1

    total = cost.aggregate(scenarios)
    if cost.requires_confirm(total) and not args.confirm_cost:
        # 预算明细:声明了 BUDGET_FIXTURE 的场景按推导预算逐项展示(0 LLM 场景无明细)
        detail = "; ".join(
            cost.describe(b) for b in (cost.budget_for(s) for s in scenarios) if b is not None
        )
        suffix = f"({detail})" if detail else ""
        print(f"拒绝执行:LLM 最坏调用预算合计 {total} 超过阈值 {cost.THRESHOLD}{suffix},"
              "需 --confirm-cost", file=sys.stderr)
        return 1
    if cost.requires_confirm(total):
        print(f"成本闸门: --confirm-cost 已确认,最坏调用预算合计 {total} 次")

    run_id = str(uuid.uuid4())
    base = environments.resolve(args.environment)
    shlogging.init_logger(run_id, Path(__file__).resolve().parents[1] / "logs" / "test-platform.log")

    failed = 0
    for mod in scenarios:
        print(f"\n===== 场景 {mod.NAME} ({mod.SUITE}) =====")
        # 会话建立前的身份为空;场景建立会话后回写 user_id
        shlogging.set_context(suite=mod.SUITE, scenario=mod.NAME, user_id="")
        args_list = ["--base-url", base, "--environment", args.environment, "--run-id", run_id]
        failed += mod.main(args_list)
    print(f"\n套件 {args.suite} 完成, 失败步骤 {failed}, run_id={run_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
