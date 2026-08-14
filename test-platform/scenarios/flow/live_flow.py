"""完整制卡流程场景(API Key → PDF → 样卡 → 任务 → 复习 → 看板 → quality-summary)端到端联调,账号 Bearer 流程。

成本闸门(DESIGN 8.3):废弃「live 固定 3 次调用」假设——LLM_CALLS 由 BUDGET_FIXTURE
按契约默认上限推导最坏调用预算(PLANNING/GENERATING/SCORING + 固定 api-key 校验与
samples);任务完成后经 GET /tasks/{id}/batches 对账实际尝试/token/成本(GENERATING
阶段账本投影,PLANNING/SCORING 无 HTTP 观测入口,边界如实声明)。API Key 从仓库根
.env 读取,仅内存使用,绝不输出。
运行方式(由 runner 调度或直接):
    python3 scenarios/flow/live_flow.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID]
    python3 scenarios/flow/live_flow.py --skip-generate   # 到样卡为止,不创建任务(无对账)
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import uuid
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/flow),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/flow/live_flow.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, cost, environments, logging as shlogging
from shanka.cleanup import DataScope
from shanka.client import Response, ShankaClient
from shanka.report import check, record, summary

NAME = "live_flow"
SUITE = "flow"

# __file__ 相对推导仓库根 .env(密钥仅内存使用,绝不输出):live_flow.py 位于
# test-platform/scenarios/flow/,parents[0]=flow/ → [1]=scenarios/ → [2]=test-platform/ → [3]=仓库根
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_POLL_INTERVAL_S = 5    # 任务轮询间隔
_POLL_TIMEOUT_S = 600   # 任务轮询总上限
_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
_GEN_CONFIG = {
    "quantity_tendency": "BALANCED",
    "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
}

# 受控 fixture:固定取前 2 章、BALANCED 密度;最坏调用预算由 cost.derive_budget 推导
# (废弃「live 固定 3 次调用」假设——LLM_CALLS 为推导值,非手写常量)。
# 前提声明:PLANNING 按 1 规划组计(前 2 章累计页文本 ≤ planner_max_input_chars 20k);
# 页文本量超过 20k 时后端拆组(上限 30 组),实际 PLANNING 调用与成本高于推导值(欠报方向)
BUDGET_FIXTURE = {"chapters": 2, "quantity_tendency": _GEN_CONFIG["quantity_tendency"],
                  "generate": True}
LIVE_BUDGET = cost.derive_budget(**BUDGET_FIXTURE)
LLM_CALLS = LIVE_BUDGET.total_calls()


def _body(r: Response) -> dict:
    """安全取响应 JSON 字典(非 dict/解析失败时返回空字典)。"""
    return r.json if isinstance(r.json, dict) else {}


def _sample_cards(body: dict) -> list[dict]:
    """样卡数组兼容多字段名(samples/sample_cards/cards/items/data)。"""
    for key in ("samples", "sample_cards", "cards", "items", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return []


def _load_env_key() -> str:
    """从 .env 正则解析 DEEPSEEK_API_KEY;缺失报错退出(密钥不入日志/print/命令行)。"""
    try:
        text = _ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        raise SystemExit(f"无法读取 {_ENV_FILE}(缺失 DEEPSEEK_API_KEY)") from None
    for line in text.splitlines():
        m = re.match(r"DEEPSEEK_API_KEY\s*=\s*(.+)", line.strip())
        if m:
            key = m.group(1).strip().strip('"').strip("'")
            if key:
                return key
    raise SystemExit(f"未在 {_ENV_FILE} 找到 DEEPSEEK_API_KEY")


def run(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    password: str,
    api_key: str,
    run_id: str,
    skip_generate: bool,
    keep: bool,
) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 0. 会话建立(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username, password=password)
    check("建立会话(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0
    scope = DataScope(c)

    # 1. API Key 保存(幂等)与状态(密钥仅内存使用;client 对 PUT /api-key 自动脱敏不记录,红线 4)
    r = c.request("PUT", "/api-key", body={"api_key": api_key}, idempotent=True, step="api-key-put")
    check("PUT /api-key -> 200", r.status == 200, f"({r.status})")
    r = c.request("GET", "/api-key/status", step="api-key-status")
    body = _body(r)
    check("GET /api-key/status -> AVAILABLE", body.get("status") == "AVAILABLE",
          f"status={body.get('status')}")

    # 2. 复用当前用户已解析 PDF:列表选第一个 PARSED,取章节断言非空
    r = c.request("GET", "/pdfs", step="pdf-list")
    check("GET /pdfs -> 200", r.status == 200, f"({r.status})")
    pdfs = [p for p in _body(r).get("items", []) if p.get("status") == "PARSED"]
    check("存在已解析 PDF", bool(pdfs), f"parsed={len(pdfs)}")
    if not pdfs:
        raise SystemExit(
            f"当前用户无已解析 PDF(parsed=0):请先上传解析 PDF 或预置测试账号数据后重跑"
        )
    file_id = pdfs[0]["file_id"]
    r = c.request("GET", f"/pdfs/{file_id}", step="pdf-detail")
    check("GET /pdfs/{file_id} -> 200", r.status == 200, f"({r.status})")
    chapters = [ch for ch in (_body(r).get("chapters") or [])]
    check("PDF 章节非空", bool(chapters), f"chapters={len(chapters)}")
    if not chapters:
        raise SystemExit(f"PDF {file_id[:8]} 无章节,无法生成")
    sel = chapters[:2]

    # 3. 样卡预览(无副作用、豁免幂等键,故不幂等)
    r = c.request("POST", "/samples",
                  body={"file_id": file_id,
                        "chapter_ids": [ch["chapter_id"] for ch in sel],
                        "generation_config": _GEN_CONFIG},
                  step="samples")
    check("POST /samples -> 200", r.status == 200, f"({r.status})")
    cards = _sample_cards(_body(r))
    check("样卡数组非空", bool(cards), f"cards={len(cards)}")
    if not cards:
        raise SystemExit("样卡生成为空,无法继续")

    # 4. 创建牌组 + 生成任务(--skip-generate 时到此结束,仍需注销会话)
    if skip_generate:
        print("    [skip-generate] 跳过 POST /tasks 与评级,流程到样卡为止")
        r = c.logout()
        check("logout -> 204", r.status == 204, f"({r.status})")
        if created:
            record("local_test_users_created", created)
        return summary()

    try:
        deck_id = scope.create_deck("联调测试牌组")
    except RuntimeError as exc:
        raise SystemExit(f"创建牌组失败: {exc}") from None
    check("POST /decks 创建联调测试牌组 -> 201", True, f"deck={deck_id[:8]}")

    r = c.request("POST", "/tasks",
                  body={"file_id": file_id, "deck_id": deck_id,
                        "chapter_ids": [ch["chapter_id"] for ch in sel],
                        "generation_config": _GEN_CONFIG},
                  idempotent=True, step="task-create")
    check("POST /tasks -> 201", r.status == 201, f"({r.status})")
    task_id = _body(r).get("task_id")
    check("任务返回 task_id", isinstance(task_id, str), f"task={str(task_id)[:8]}")
    if not isinstance(task_id, str):
        raise SystemExit(f"任务创建响应缺 task_id: {_body(r)}")

    # 轮询至终态:间隔 5s,单次请求 60s 超时(client timeout),总上限 600s
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    status = ""
    while status not in _TERMINAL:
        r = c.request("GET", f"/tasks/{task_id}", step="task-poll")
        body = _body(r)
        status = str(body.get("status") or "")
        check("轮询 GET /tasks/{id} -> 200", r.status == 200,
              f"status={status} cards={body.get('generated_card_count')}")
        if r.status != 200:
            raise SystemExit(f"轮询任务 {task_id[:8]} 失败: HTTP {r.status}")
        if status in _TERMINAL:
            break
        if time.monotonic() >= deadline:
            raise SystemExit(f"任务 {task_id[:8]} 轮询超时(>{_POLL_TIMEOUT_S}s),最后状态 {status!r}")
        time.sleep(_POLL_INTERVAL_S)
    check("任务终态 COMPLETED", status == "COMPLETED", f"status={status}")
    if status != "COMPLETED":
        raise SystemExit(f"任务未完成: status={status} "
                         f"stage={body.get('failure_stage')} error_code={body.get('error_code')}")
    check("任务生成卡片数 > 0", bool(body.get("generated_card_count")),
          f"cards={body.get('generated_card_count')}")

    # 4.5 成本对账(DESIGN 8.3):GENERATING 阶段经 batches 观测(批=单元账本投影);
    # PLANNING/SCORING 尝试数无 HTTP 观测入口(llm_call_attempts 无 GET 端点),边界如实声明
    r = c.request("GET", f"/tasks/{task_id}/batches", step="task-batches")
    check("对账: GET /tasks/{id}/batches -> 200", r.status == 200, f"({r.status})")
    if r.status == 200:
        rec = cost.reconcile(_body(r).get("items") or [], LIVE_BUDGET)
        check("对账: 生成尝试/批数在预算内", rec.within_budget, rec.usage_line)
        record("llm_budget_calls", LIVE_BUDGET.total_calls())
        record("llm_attempts_actual", rec.generation_attempts)
        record("llm_tokens_actual", rec.tokens)
        record("llm_cost_actual", rec.cost_yuan)
        record("llm_budget_premise",
               "PLANNING 1 规划组前提:前 2 章累计页文本 ≤ 20k(planner_max_input_chars),超出则拆组实际调用高于推导值")
        print(f"    [对账] 预算: {cost.describe(LIVE_BUDGET)}")
        print(f"    [对账] 实际(GENERATING 阶段,批=单元投影): {rec.usage_line}")
        print("    [对账] 边界: PLANNING/SCORING 尝试数无 HTTP 观测入口,仅 GENERATING 可对账")
        print("    [对账] 前提: PLANNING 按 1 规划组计(前 2 章累计页文本 ≤ 20k),超出则拆组、实际调用高于推导值")

    # 5. 牌组卡片 + 复习评级(任意状态可评级,C-06)
    r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards")
    check("GET /decks/{id}/cards -> 200", r.status == 200, f"({r.status})")
    items = [it for it in _body(r).get("items", [])]
    check("牌组卡片非空", bool(items), f"cards={len(items)}")
    if not items:
        raise SystemExit(f"牌组 {deck_id[:8]} 无卡片,无法评级")
    card_id = items[0]["card_id"]
    r = c.request("POST", "/review-events",
                  body={"card_id": card_id, "rating": "GOOD",
                        "client_event_id": str(uuid.uuid4()),
                        "device_timezone": "Asia/Shanghai"},
                  idempotent=True, step="review-event")
    body = _body(r)
    check("POST /review-events -> 200", r.status == 200, f"({r.status})")
    check("评级返回 state/due", isinstance(body.get("state"), str) and bool(body.get("due")),
          f"state={body.get('state')} due={body.get('due')}")

    # 6. 看板(时区对齐,观察本周统计)
    r = c.request("GET", "/stats/dashboard?timezone=Asia/Shanghai", step="dashboard")
    body = _body(r)
    check("GET /stats/dashboard -> 200", r.status == 200, f"({r.status})")
    check("看板含周复习/掌握统计",
          "weekly_total" in body and "mastered_card_count" in body,
          f"weekly={body.get('weekly_total')} mastered={body.get('mastered_card_count')}")

    # 7. 观测:quality-summary 按 user(Bearer 主体,跨用户不泄漏)
    r = c.request("GET", "/observability/quality-summary", step="quality-summary")
    body = _body(r)
    check("GET /observability/quality-summary -> 200", r.status == 200, f"({r.status})")
    groups = body.get("groups") if isinstance(body.get("groups"), list) else None
    check("summary 形状(group_by/days/groups)",
          bool(body.get("group_by")) and isinstance(body.get("days"), int) and isinstance(groups, list),
          str(body)[:100])
    if groups:
        check("本次生成计入当前用户 summary",
              any(isinstance(g, dict) and bool(g.get("card_count")) for g in groups),
              f"groups={len(groups)}")
    # 本地交叉断言:临时新账号(无生成)summary 必须为空——observability 按 user 隔离
    if not environments.is_prod(environment):
        obs_name = account.temp_username(run_id, "obs")
        obs = account.bootstrap(c, environment=environment, username=obs_name,
                                password=account.temp_password())
        check("观测临时账号建立", obs is not None, f"user={obs_name}")
        if obs is None:
            # 异常路径:bootstrap 失败时本地 token 状态不承诺——切回主账号确定性后续;
            # 临时账号 session 若已建而 client 未持有 token 则无法撤销,仅 WARN 登记(对齐 isolation.py)
            print(f"    [warn] 观测临时账号 {obs_name} 建立失败,其会话可能未撤销"
                  f"(注册失败路径,无 token 可注销)")
        else:
            created += 1
            r = c.request("GET", "/observability/quality-summary", step="obs-quality-summary")
            body = _body(r)
            check("临时账号 quality-summary -> 200", r.status == 200, f"({r.status})")
            check("临时账号 summary 为空(按 user 隔离)", body.get("groups") == [], str(body)[:100])
            r = c.logout()
            check("临时账号 logout -> 204", r.status == 204, f"({r.status})")
        c.set_token(session["access_token"])

    # 8. 清理(--keep 时保留牌组)+ 注销会话
    if not keep:
        scope.cleanup()
        r = c.request("GET", f"/decks/{deck_id}", step="deck-after-cleanup")
        check("清理后牌组不可见(404)", r.status == 404, f"({r.status})")
    else:
        print("    [keep] 保留测试牌组,不清理")
    r = c.logout()
    check("logout -> 204", r.status == 204, f"({r.status})")

    if created:
        record("local_test_users_created", created)
    return summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--run-id", default=None, help="runner 注入(临时观测账号命名);直跑时自动生成")
    ap.add_argument("--skip-generate", action="store_true", help="跳过 POST /tasks 与评级,到样卡为止")
    ap.add_argument("--keep", action="store_true", help="不清理创建的牌组")
    args = ap.parse_args(argv)

    try:
        username, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1

    # 密钥仅内存使用;client 对 PUT /api-key 自动脱敏不记录(红线 4)
    api_key = _load_env_key()
    # 任务生成期后端事件循环阻塞,轮询请求需长超时(默认 30s 不足)
    c = ShankaClient(args.base_url, timeout=60)
    return run(c, environment=args.environment, username=username, password=password,
               api_key=api_key, run_id=args.run_id or str(uuid.uuid4()),
               skip_generate=args.skip_generate, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
