"""完整制卡流程场景(API Key → 项目 → 任务(样卡/生成) → 复习 → 看板 → quality-summary)端到端联调,账号 Bearer 流程。

V2.5 化(终审 I-1):POST /samples 与 POST /tasks 已随 V2.5 下线——样卡持久化在
tasks 域下(POST /tasks/{id}/samples),任务创建入口为 POST /projects/{project_id}/tasks;
牌组经 POST /decks 带 project_id 归属项目(C-1 生产写路径)。
成本闸门(DESIGN 8.3):废弃「live 固定 3 次调用」假设——LLM_CALLS 由 BUDGET_FIXTURE
按契约默认上限推导最坏调用预算(PLANNING/GENERATING/SCORING + 固定 api-key 校验与
samples);任务完成后经 GET /tasks/{id}/batches 对账实际尝试/token/成本(GENERATING
阶段账本投影,PLANNING/SCORING 无 HTTP 观测入口,边界如实声明)。API Key 从仓库根
.env 读取,仅内存使用,绝不输出。
运行方式(由 runner 调度或直接):
    python3 scenarios/flow/live_flow.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID]
    python3 scenarios/flow/live_flow.py --skip-generate   # 到项目/章节选择为止,不创建任务(无对账)
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

from shanka import account, client, cost, environments, logging as shlogging
from shanka.cleanup import DataScope
from shanka.client import Response, ShankaClient
from shanka.report import check, record, summary

NAME = "live_flow"
SUITE = "flow"

# __file__ 相对推导仓库根 .env(密钥仅内存使用,绝不输出):live_flow.py 位于
# test-platform/scenarios/flow/,parents[0]=flow/ → [1]=scenarios/ → [2]=test-platform/ → [3]=仓库根
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_POLL_INTERVAL_S = 5    # 任务轮询间隔
_SAMPLES_TIMEOUT_S = 300  # 样卡生成(1~3 张)后台完成上限
_POLL_TIMEOUT_S = 600   # 生成轮询总上限
_HARD_TERMINAL = ("FAILED", "ABANDONED")
_GEN_CONFIG = {
    "coverage_mode": "BALANCED",
    "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
}

# 受控 fixture:固定取前 2 章、BALANCED 密度;最坏调用预算由 cost.derive_budget 推导
# (废弃「live 固定 3 次调用」假设——LLM_CALLS 为推导值,非手写常量)。
# PLANNING 组数前提(V2.4 fixture 锚定):样书前 2 章 42.6k 字符 ÷ planner_max_input_chars
# 20k(config.py)= 3 组向上取整;组数由 fixture 显式声明(planning_groups),
# 实际组数受后端 max_planner_groups_per_task=30 上限;调整样书或阈值需同步声明
BUDGET_FIXTURE = {"chapters": 2, "quantity_tendency": "BALANCED",
                  "generate": True, "planning_groups": 3}
LIVE_BUDGET = cost.derive_budget(**BUDGET_FIXTURE)
LLM_CALLS = LIVE_BUDGET.total_calls()


def _body(r: Response) -> dict:
    """安全取响应 JSON 字典(非 dict/解析失败时返回空字典)。"""
    return r.json if isinstance(r.json, dict) else {}


def _poll_task(c: ShankaClient, task_id: str, *, target: str, timeout_s: int,
               step: str = "task-poll") -> dict:
    """轮询 GET /tasks/{id} 至 target;FAILED/ABANDONED 或超时抛 SystemExit(非零退出)。

    样卡阶段 target=AWAITING_SAMPLE_CONFIRMATION,生成阶段 target=COMPLETED
    (V2.5 样卡持久化于任务;与 v25_core_flow 同款轮询)。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        r = c.request("GET", f"/tasks/{task_id}", step=step)
        if r.status != 200:
            raise SystemExit(f"轮询任务 {task_id[:8]} 失败: HTTP {r.status}")
        body = _body(r)
        status = str(body.get("status") or "")
        if status == target:
            return body
        if status in _HARD_TERMINAL:
            raise SystemExit(
                f"任务未达 {target}: status={status} "
                f"failure_stage={body.get('failure_stage')} error_code={body.get('error_code')}"
            )
        if time.monotonic() >= deadline:
            raise SystemExit(f"任务 {task_id[:8]} 轮询超时(>{timeout_s}s),最后状态 {status!r}")
        time.sleep(_POLL_INTERVAL_S)


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
    email: str,
    password: str,
    api_key: str,
    run_id: str,
    skip_generate: bool,
    keep: bool,
) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")

    # 0. 会话建立(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username,
                                email=email, password=password)
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

    # 2. 项目前置(V2.5):复用测试账号已有 READY 项目(与 v25_core_flow 同约定;
    #    POST /samples 与 POST /tasks 已随 V2.5 下线——样卡持久化在 tasks 域下)
    r = c.request("GET", "/projects", step="project-list")
    check("GET /projects -> 200", r.status == 200, f"({r.status})")
    ready = [p for p in _body(r).get("items", []) if p.get("status") == "READY"]
    check("存在 READY 项目(前置)", bool(ready), f"ready={len(ready)}")
    if not ready:
        raise SystemExit("当前用户无 READY 项目:请先上传解析并确认章节的 PDF(或预置测试账号数据)后重跑")
    project_id = ready[0]["project_id"]
    r = c.request("GET", f"/projects/{project_id}", step="project-detail")
    check("GET /projects/{id} -> 200", r.status == 200, f"({r.status})")
    chapters = [ch for ch in (_body(r).get("file") or {}).get("chapters") or [] if isinstance(ch, dict)]
    check("项目章节 >= 2(前置)", len(chapters) >= 2, f"chapters={len(chapters)}")
    if len(chapters) < 2:
        raise SystemExit(f"项目 {project_id[:8]} 章节不足 2,无法覆盖生成链路(当前 {len(chapters)})")
    sel = chapters[:2]

    # 3. 创建项目归属牌组 + DRAFT 任务(--skip-generate 时到此结束,仍需注销会话)
    if skip_generate:
        print("    [skip-generate] 跳过任务创建与评级,流程到项目/章节选择为止")
        r = c.logout()
        check("logout -> 204", r.status == 204, f"({r.status})")
        if created:
            record("local_test_users_created", created)
        return summary()

    try:
        deck_id = scope.create_deck("联调测试牌组", project_id=project_id)
    except RuntimeError as exc:
        raise SystemExit(f"创建牌组失败: {exc}") from None
    check("POST /decks 建项目归属牌组 -> 201", True, f"deck={deck_id[:8]}")

    r = client.create_task(c, project_id=project_id, deck_id=deck_id,
                           chapter_ids=[ch["chapter_id"] for ch in sel],
                           generation_config=_GEN_CONFIG)
    body = _body(r)
    check("POST /projects/{id}/tasks -> 201 DRAFT",
          r.status == 201 and body.get("status") == "DRAFT",
          f"({r.status}) {body.get('status')}")
    task_id = body.get("task_id")
    check("任务返回 task_id", isinstance(task_id, str), f"task={str(task_id)[:8]}")
    if not isinstance(task_id, str):
        raise SystemExit(f"任务创建响应缺 task_id: {body}")

    # 4. 持久化样卡(V2.5 任务域:POST /tasks/{id}/samples 后台完成)→ 确认后 start →
    #    轮询 COMPLETED(间隔 5s,单次请求 60s 超时,样卡/生成各自上限)
    r = client.request_samples(c, task_id)
    check("POST /tasks/{id}/samples -> 200", r.status == 200, f"({r.status})")
    body = _poll_task(c, task_id, target="AWAITING_SAMPLE_CONFIRMATION",
                      timeout_s=_SAMPLES_TIMEOUT_S, step="task-poll")
    sample_cards = body.get("sample_cards") if isinstance(body.get("sample_cards"), list) else []
    check("样卡持久化到任务(非空)", bool(sample_cards), f"cards={len(sample_cards)}")

    r = client.start_task(c, task_id)
    body = _body(r)
    check("POST /tasks/{id}/start -> 200 GENERATING",
          r.status == 200 and body.get("status") == "GENERATING",
          f"({r.status}) {body.get('status')}")
    body = _poll_task(c, task_id, target="COMPLETED", timeout_s=_POLL_TIMEOUT_S,
                      step="task-poll")
    check("任务终态 COMPLETED", body.get("status") == "COMPLETED",
          f"status={body.get('status')}")
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
               "PLANNING 按 fixture 声明 3 规划组计(样书前 2 章 42.6k 字符 ÷ 20k 向上取整),"
               "实际组数受后端 max_planner_groups_per_task=30 上限")
        print(f"    [对账] 预算: {cost.describe(LIVE_BUDGET)}")
        print(f"    [对账] 实际(GENERATING 阶段,批=单元投影): {rec.usage_line}")
        print("    [对账] 边界: PLANNING/SCORING 尝试数无 HTTP 观测入口,仅 GENERATING 可对账")
        print("    [对账] 前提: PLANNING 按 fixture 声明 3 规划组计(前 2 章 42.6k 字符 ÷ 20k 向上取整),"
              "实际组数受后端 max_planner_groups_per_task=30 上限")

    # 5. 牌组卡片 + 复习评级(任意状态可评级,C-06;V2.5 响应 review_state/study_date,
    #    时区由账号学习时区决定,客户端不上报 device_timezone)
    r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards")
    check("GET /decks/{id}/cards -> 200", r.status == 200, f"({r.status})")
    items = [it for it in _body(r).get("items", [])]
    check("牌组卡片非空", bool(items), f"cards={len(items)}")
    if not items:
        raise SystemExit(f"牌组 {deck_id[:8]} 无卡片,无法评级")
    card_id = items[0]["card_id"]
    r = client.submit_review(c, card_id=card_id, rating="GOOD",
                             client_event_id=str(uuid.uuid4()))
    body = _body(r)
    check("POST /review-events -> 200", r.status == 200, f"({r.status})")
    rs = body.get("review_state") if isinstance(body.get("review_state"), dict) else {}
    check("评级返回 review_state/study_date",
          bool(rs.get("state")) and bool(body.get("study_date")),
          f"state={rs.get('state')}")

    # 6. 看板(V2.5 服务端按账号学习时区分桶,无 timezone 查询参数)
    r = c.request("GET", "/stats/dashboard", step="dashboard")
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
                                email=account.temp_email(run_id, "obs"),
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
    ap.add_argument("--skip-generate", action="store_true",
                    help="跳过任务创建与评级,到项目/章节选择为止")
    ap.add_argument("--keep", action="store_true", help="不清理创建的牌组")
    args = ap.parse_args(argv)

    try:
        username, email, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1

    # 密钥仅内存使用;client 对 PUT /api-key 自动脱敏不记录(红线 4)
    api_key = _load_env_key()
    # 任务生成期后端事件循环阻塞,轮询请求需长超时(默认 30s 不足)
    c = ShankaClient(args.base_url, timeout=60)
    return run(c, environment=args.environment, username=username, email=email,
               password=password, api_key=api_key, run_id=args.run_id or str(uuid.uuid4()),
               skip_generate=args.skip_generate, keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
