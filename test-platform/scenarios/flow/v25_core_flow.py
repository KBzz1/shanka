"""V2.5 非可视化 Release 主链路场景(账号/偏好 → 项目/PDF/章节 → 持久化样卡任务 → 完整生成 →
今日评级 → 两阶段重写 → 删除撤销 → 真实统计),对 HTTP 的黑盒断言。

覆盖 PRD V2.5 release-and-acceptance 第 3 节主链路在 HTTP 层可断言部分:
- 资料与偏好:GET/PATCH /auth/me(改昵称/头像,断言后立即恢复原值)、GET/PATCH /preferences
  (每日目标/当前项目,保持至流程末用于统计跨断言,结束后还原);
- 项目/PDF/章节:复用测试账号既有 READY 项目(沿用 live_flow「复用已解析数据」约定,
  平台无 multipart 上传能力,PDF 上传/解析属未实装 pdf 域场景);项目重命名与章节改名
  (断言后恢复)、学习设置新卡章节范围(PATCH 后恢复);
- 持久化样卡任务:POST /projects/{project_id}/tasks(DRAFT 自动保存)→ POST /tasks/{id}/samples
  (样卡持久化于任务,非临时返回)→ POST /tasks/{id}/start → 轮询 COMPLETED;
- 今日评级:POST /review-events 四档评级 + 同键幂等重放(不重复计数)、GET /study/today;
- 两阶段重写:preview→apply(版本 CAS 递增、内容=预览)与 preview→cancel(原卡内容/版本不变);
- 删除撤销:DELETE /cards/{id} → 10 秒删除批次(卡片不可见)→ pending 恢复 →
  undo(卡片恢复可见);
- 真实统计:GET /stats/dashboard 周目标 = 每日目标 × 7、时区 = 账号学习时区、本周复习 ≥ 1。

成本闸门(DESIGN 8.3):LLM_CALLS = cost.derive_budget 推导最坏预算(2 章 BALANCED,
3 规划组,59 次)+ 重写预览 2 次(apply/cancel 各 1,预算模型不含重写,显式补充)= 61,
超阈值必须 --confirm-cost。任务完成后经 GET /tasks/{id}/batches 对账(GENERATING 阶段
账本投影;PLANNING/SCORING 尝试数无 HTTP 观测入口,边界如实声明)。
API Key 从仓库根 .env 读取(DEEPSEEK_API_KEY),仅内存使用,绝不输出(红线 4);
卡片内容仅存在于响应,不进入任何 check 明细与日志。

前置条件:测试账号需已有 READY 项目且章节 ≥ 2(缺失时明确报错退出,不自动构造)。
运行方式(由 runner 调度或直接):
    python3 scenarios/flow/v25_core_flow.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID] [--keep]
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
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/flow/v25_core_flow.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, cost, environments, logging as shlogging
from shanka import client
from shanka.client import ShankaClient
from shanka.report import check, record, summary

NAME = "v25_core_flow"
SUITE = "flow"

_POLL_INTERVAL_S = 5
_SAMPLES_TIMEOUT_S = 300   # 样卡生成(1~3 张)后台完成上限
_GENERATION_TIMEOUT_S = 600
_HARD_TERMINAL = ("FAILED", "ABANDONED")
_GEN_CONFIG = {
    "coverage_mode": "BALANCED",
    "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
}
_AVATARS = [f"mood_{i:02d}" for i in range(1, 13)]

# 受控 fixture:固定取前 2 章、BALANCED 密度;最坏调用预算由 cost.derive_budget 推导。
# PLANNING 组数前提(V2.4 fixture 锚定):样书前 2 章 42.6k 字符 ÷ planner_max_input_chars
# 20k(config.py)= 3 组向上取整;实际组数受后端 max_planner_groups_per_task=30 上限。
BUDGET_FIXTURE = {"chapters": 2, "quantity_tendency": "BALANCED",
                  "generate": True, "planning_groups": 3}
LIVE_BUDGET = cost.derive_budget(**BUDGET_FIXTURE)
# 两阶段重写 preview→apply 与 preview→cancel 各 1 次真实重写调用(推导模型不含重写,显式补充)
REWRITE_PREVIEW_CALLS = 2
LLM_CALLS = LIVE_BUDGET.total_calls() + REWRITE_PREVIEW_CALLS


# __file__ 相对推导仓库根 .env(密钥仅内存使用,绝不输出):v25_core_flow.py 位于
# test-platform/scenarios/flow/,parents[0]=flow/ → [1]=scenarios/ → [2]=test-platform/ → [3]=仓库根
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


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


def _other_avatar(current: str) -> str:
    """选择与当前不同的内置 Moods 预设头像(12 选 1)。"""
    for key in _AVATARS:
        if key != current:
            return key
    return "mood_01"


def _next_goal(current: int | None) -> int:
    """与当前不同的 10 的倍数每日目标(契约 10~200 区间;异常值回落 50)。"""
    if not isinstance(current, int) or current % 10 != 0 or current < 10 or current > 200:
        return 50
    return current + 10 if current + 10 <= 200 else current - 10


def _poll_task(c: ShankaClient, task_id: str, *, target: str, timeout_s: int,
               step: str = "task-poll") -> dict:
    """轮询 GET /tasks/{id} 至 target;FAILED/ABANDONED 或超时抛 SystemExit(非零退出)。

    样卡阶段 target=AWAITING_SAMPLE_CONFIRMATION,生成阶段 target=COMPLETED;
    后台 worker 失败(Failed)时失败信息经 SystemExit 消息透出(无 fake PASS)。
    """
    deadline = time.monotonic() + timeout_s
    while True:
        r = c.request("GET", f"/tasks/{task_id}", step=step)
        if r.status != 200:
            raise SystemExit(f"轮询任务 {task_id[:8]} 失败: HTTP {r.status}")
        body = client.body(r)
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


def run(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    email: str,
    password: str,
    api_key: str,
    run_id: str,
    keep: bool,
) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")
    print(f"    [成本] 本场景最坏调用预算 = 推导 {LIVE_BUDGET.total_calls()} + 重写预览 "
          f"{REWRITE_PREVIEW_CALLS} = {LLM_CALLS} 次(超阈值需 --confirm-cost)")

    # 0. 会话建立(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username,
                                email=email, password=password)
    check("建立会话(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0

    # 1. API Key 保存(幂等)与状态(密钥仅内存使用;client 对 PUT /api-key 自动脱敏不记录,红线 4)
    r = c.request("PUT", "/api-key", body={"api_key": api_key}, idempotent=True,
                  step="api-key-put")
    check("PUT /api-key -> 200", r.status == 200, f"({r.status})")
    r = c.request("GET", "/api-key/status", step="api-key-status")
    check("GET /api-key/status -> AVAILABLE", client.body(r).get("status") == "AVAILABLE",
          f"status={client.body(r).get('status')}")

    # 2. 资料:改昵称/头像断言后立即恢复(共享账号不留变更)
    r = c.request("GET", "/auth/me", step="me")
    check("GET /auth/me -> 200", r.status == 200, f"({r.status})")
    me = client.body(r).get("user") if isinstance(client.body(r), dict) else {}
    check("me 含 email/avatar_key", bool(me.get("email")) and bool(me.get("avatar_key")),
          "email/avatar_key 存在(字段值不外泄)")
    orig_username = me.get("username") or username
    orig_avatar = me.get("avatar_key") or "mood_01"
    new_username = f"{orig_username[:12]}-v25"
    new_avatar = _other_avatar(orig_avatar)
    r = c.request("PATCH", "/auth/me", body={"username": new_username, "avatar_key": new_avatar},
                  idempotent=True, step="me-update")
    check("PATCH /auth/me 改昵称/头像 -> 200", r.status == 200, f"({r.status})")
    r = c.request("PATCH", "/auth/me", body={"username": orig_username, "avatar_key": orig_avatar},
                  idempotent=True, step="me-restore")
    check("恢复昵称/头像", r.status == 200, f"({r.status})")

    # 3. 偏好读取(原值捕获;每日目标/当前项目变更保持至流程末,finally 还原)
    r = c.request("GET", "/preferences", step="prefs-get")
    check("GET /preferences -> 200", r.status == 200, f"({r.status})")
    prefs = client.body(r)
    check("偏好形状(coverage_mode/时区/每日目标)",
          bool(prefs.get("learning_timezone")) and isinstance(prefs.get("daily_learning_goal"), int),
          str(prefs)[:80])
    orig_goal = prefs.get("daily_learning_goal") if isinstance(prefs.get("daily_learning_goal"), int) else 50
    orig_current_project = prefs.get("current_project_id") if isinstance(
        prefs.get("current_project_id"), str) else None

    prefs_mutated = False
    task_id: str | None = None
    deck_id: str | None = None
    try:
        # 4. 项目前置(READY 项目/章节):失败在变更之前,不留变更
        r = c.request("GET", "/projects", step="project-list")
        check("GET /projects -> 200", r.status == 200, f"({r.status})")
        ready = [p for p in client.body(r).get("items", []) if p.get("status") == "READY"]
        check("存在 READY 项目(前置)", bool(ready), f"ready={len(ready)}")
        if not ready:
            raise SystemExit("当前用户无 READY 项目:请先上传解析并确认章节的 PDF(或预置测试账号数据)后重跑")
        project_id = ready[0]["project_id"]
        r = c.request("GET", f"/projects/{project_id}", step="project-detail")
        check("GET /projects/{id} -> 200", r.status == 200, f"({r.status})")
        detail = client.body(r)
        chapters = [ch for ch in (detail.get("file") or {}).get("chapters") or [] if isinstance(ch, dict)]
        check("项目章节 >= 2(前置)", len(chapters) >= 2, f"chapters={len(chapters)}")
        if len(chapters) < 2:
            raise SystemExit(f"项目 {project_id[:8]} 章节不足 2,无法覆盖生成链路(当前 {len(chapters)})")
        chapter_ids = [ch["chapter_id"] for ch in chapters[:2]]

        # 5. 偏好变更(每日目标/当前项目)——跨断言期间保持,finally 还原
        goal = _next_goal(orig_goal)
        r = c.request("PATCH", "/preferences", body={"daily_learning_goal": goal},
                      idempotent=True, step="prefs-goal")
        check("PATCH /preferences 每日目标 -> 200", r.status == 200, f"({r.status})")
        prefs_mutated = True

        # 6. 项目/PDF/章节:重命名、章节改名、学习设置(PATCH 断言后立即恢复)
        orig_name = detail.get("name") or ready[0].get("name") or "学习项目"
        new_name = f"{orig_name[:44]}-v25"
        r = c.request("PATCH", f"/projects/{project_id}", body={"name": new_name},
                      idempotent=True, step="project-rename")
        check("PATCH /projects/{id} 重命名 -> 200", r.status == 200, f"({r.status})")
        r = c.request("PATCH", f"/projects/{project_id}", body={"name": orig_name},
                      idempotent=True, step="project-rename-restore")
        check("恢复项目名", r.status == 200, f"({r.status})")

        ch0 = chapters[0]
        orig_ch_name = ch0.get("name") or "章节"
        new_ch_name = f"{orig_ch_name[:40]}-v25"
        r = c.request("PATCH", f"/projects/{project_id}/chapters/{ch0['chapter_id']}",
                      body={"name": new_ch_name}, idempotent=True, step="chapter-rename")
        check("PATCH 章节改名 -> 200", r.status == 200, f"({r.status})")
        r = c.request("PATCH", f"/projects/{project_id}/chapters/{ch0['chapter_id']}",
                      body={"name": orig_ch_name}, idempotent=True, step="chapter-rename-restore")
        check("恢复章节名", r.status == 200, f"({r.status})")

        r = c.request("GET", f"/projects/{project_id}/study-settings", step="study-settings-get")
        check("GET study-settings -> 200", r.status == 200, f"({r.status})")
        ss = client.body(r)
        check("学习设置形状(章节范围/未归属开关)",
              isinstance(ss.get("selected_new_card_chapter_ids"), list)
              and isinstance(ss.get("include_unassigned"), bool),
              str(ss)[:80])
        r = c.request("PATCH", f"/projects/{project_id}/study-settings",
                      body={"selected_new_card_chapter_ids": [ch0["chapter_id"]],
                            "include_unassigned": True},
                      idempotent=True, step="study-settings-update")
        check("PATCH study-settings 新卡范围 -> 200", r.status == 200, f"({r.status})")
        r = c.request("PATCH", f"/projects/{project_id}/study-settings",
                      body={"selected_new_card_chapter_ids": ss.get("selected_new_card_chapter_ids") or [],
                            "include_unassigned": bool(ss.get("include_unassigned"))},
                      idempotent=True, step="study-settings-restore")
        check("恢复学习设置", r.status == 200, f"({r.status})")

        r = c.request("PATCH", "/preferences", body={"current_project_id": project_id},
                      idempotent=True, step="prefs-project")
        check("PATCH 偏好当前项目 -> 200", r.status == 200, f"({r.status})")

        # 7. 项目归属牌组 + DRAFT 自动保存任务
        r = c.request("POST", "/decks", body={"name": f"v25-联调-{run_id[:6]}",
                                              "project_id": project_id},
                      idempotent=True, step="deck-create")
        check("POST /decks 建项目牌组 -> 201", r.status == 201, f"({r.status})")
        deck_id = client.body(r).get("deck_id")
        if not isinstance(deck_id, str):
            raise SystemExit(f"建牌组响应缺 deck_id: {client.body(r)}")

        r = client.create_task(c, project_id=project_id, deck_id=deck_id,
                               chapter_ids=chapter_ids, generation_config=_GEN_CONFIG)
        body = client.body(r)
        check("POST /projects/{id}/tasks -> 201 DRAFT", r.status == 201 and body.get("status") == "DRAFT",
              f"({r.status}) {body.get('status')}")
        task_id = body.get("task_id")
        if not isinstance(task_id, str):
            raise SystemExit(f"任务创建响应缺 task_id: {body}")
        r = c.request("GET", f"/tasks/{task_id}", step="task-get")
        body = client.body(r)
        check("GET /tasks/{id} 自动保存可读(DRAFT)",
              body.get("status") == "DRAFT" and isinstance(body.get("selected_chapters"), list),
              f"status={body.get('status')}")

        # 8. 持久化样卡:请求 → 轮询至 AWAITING_SAMPLE_CONFIRMATION(样卡持久化于任务)
        r = client.request_samples(c, task_id)
        check("POST /tasks/{id}/samples -> 200", r.status == 200, f"({r.status})")
        body = _poll_task(c, task_id, target="AWAITING_SAMPLE_CONFIRMATION",
                          timeout_s=_SAMPLES_TIMEOUT_S)
        sample_cards = body.get("sample_cards") if isinstance(body.get("sample_cards"), list) else []
        check("样卡持久化到任务(非空)", bool(sample_cards), f"cards={len(sample_cards)}")
        check("样卡配置指纹已记录", bool(body.get("sample_config_hash")), "")

        # 9. start → 轮询 COMPLETED(生成任务离开页面后经任务区继续查看)
        r = client.start_task(c, task_id)
        body = client.body(r)
        check("POST /tasks/{id}/start -> 200 GENERATING",
              r.status == 200 and body.get("status") == "GENERATING",
              f"({r.status}) {body.get('status')}")
        body = _poll_task(c, task_id, target="COMPLETED", timeout_s=_GENERATION_TIMEOUT_S)
        check("任务终态 COMPLETED", body.get("status") == "COMPLETED", f"status={body.get('status')}")
        check("任务生成卡片数 > 0", bool(body.get("generated_card_count")),
              f"cards={body.get('generated_card_count')}")

        # 9.5 成本对账(DESIGN 8.3):GENERATING 阶段经 batches 观测(批=单元账本投影);
        # PLANNING/SCORING 尝试数无 HTTP 观测入口,边界如实声明
        r = c.request("GET", f"/tasks/{task_id}/batches", step="task-batches")
        check("对账: GET /tasks/{id}/batches -> 200", r.status == 200, f"({r.status})")
        if r.status == 200:
            rec = cost.reconcile(client.body(r).get("items") or [], LIVE_BUDGET)
            check("对账: 生成尝试/批数在预算内", rec.within_budget, rec.usage_line)
            record("llm_budget_calls", LIVE_BUDGET.total_calls())
            record("llm_attempts_actual", rec.generation_attempts)
            record("llm_tokens_actual", rec.tokens)
            record("llm_cost_actual", rec.cost_yuan)
            record("llm_budget_premise",
                   "PLANNING 按 fixture 声明 3 规划组计(样书前 2 章 42.6k 字符 ÷ 20k 向上取整),"
                   "实际组数受后端 max_planner_groups_per_task=30 上限;"
                   f"重写预览 {REWRITE_PREVIEW_CALLS} 次另计 LLM_CALLS")
            print(f"    [对账] 预算: {cost.describe(LIVE_BUDGET)}")
            print(f"    [对账] 实际(GENERATING 阶段,批=单元投影): {rec.usage_line}")
            print("    [对账] 边界: PLANNING/SCORING 尝试数无 HTTP 观测入口,仅 GENERATING 可对账")

        # 10. 牌组卡片(生成结果一次性全部出现)+ 今日评级(幂等重放不重复计数)
        r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards")
        check("GET /decks/{id}/cards -> 200", r.status == 200, f"({r.status})")
        items = [it for it in client.body(r).get("items", []) if isinstance(it, dict)]
        check("牌组卡片 >= 4(评级/重写/删除覆盖)", len(items) >= 4, f"cards={len(items)}")
        if len(items) < 4:
            raise SystemExit(f"牌组 {deck_id[:8]} 生成卡片 {len(items)} < 4,"
                             "不足以覆盖评级/重写/删除步骤,请检查生成结果或预置数据")
        card_ids = [it["card_id"] for it in items]
        by_id = {it["card_id"]: it for it in items}

        review_key = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        r1 = client.submit_review(c, card_id=card_ids[0], rating="GOOD",
                                  client_event_id=event_id, idempotency_key=review_key)
        body1 = client.body(r1)
        check("POST /review-events -> 200", r1.status == 200, f"({r1.status})")
        rs = body1.get("review_state") if isinstance(body1.get("review_state"), dict) else {}
        check("评级返回 review_state/study_date",
              bool(rs.get("state")) and bool(body1.get("study_date")),
              f"state={rs.get('state')}")
        r2 = client.submit_review(c, card_id=card_ids[0], rating="GOOD",
                                  client_event_id=event_id, idempotency_key=review_key,
                                  step="review-event-replay")
        check("幂等重放返回首次结果(不重复计数)", r2.status == 200 and client.body(r2) == body1,
              f"({r2.status})")

        # 11. 今日计划(当前项目/服务端每日目标/评级计入)
        r = c.request("GET", "/study/today", step="study-today")
        body = client.body(r)
        check("GET /study/today -> 200", r.status == 200, f"({r.status})")
        cp = body.get("current_project") if isinstance(body.get("current_project"), dict) else None
        check("今日计划: 当前项目已设置", cp is not None and cp.get("project_id") == project_id,
              f"proj={cp.get('project_id') if cp else None}")
        check("今日计划: 每日目标 = 服务端偏好", body.get("daily_goal") == goal,
              f"goal={body.get('daily_goal')}")
        check("今日计划: 已评级计入今日完成(去重)", body.get("today_completed_count", 0) >= 1,
              f"completed={body.get('today_completed_count')}")
        check("今日计划: 卡片数组形状", isinstance(body.get("cards"), list), "")

        # 12. 两阶段重写:preview→apply 替换原卡(版本 CAS 递增、内容=预览)
        card_rewrite = card_ids[1]
        base = by_id[card_rewrite]
        r = client.create_rewrite_preview(c, card_rewrite, step="rewrite-preview-apply")
        body = client.body(r)
        check("POST rewrite-previews -> 201 PENDING",
              r.status == 201 and body.get("status") == "PENDING", f"({r.status}) {body.get('status')}")
        rewrite_id = body.get("rewrite_id")
        preview_front, preview_back = body.get("front"), body.get("back")
        check("预览 base_card_version 与当前一致", body.get("base_card_version") == base.get("version"),
              f"base={body.get('base_card_version')}")
        check("预览内容非空", bool(preview_front) and bool(preview_back), "")
        r = client.apply_rewrite_preview(c, card_rewrite, rewrite_id, step="rewrite-apply")
        body = client.body(r)
        check("apply -> 200 替换原卡", r.status == 200 and body.get("card_id") == card_rewrite,
              f"({r.status})")
        check("apply 后版本递增(CAS)", body.get("version") != base.get("version"),
              f"v={body.get('version')}")
        check("apply 后内容 = 预览内容",
              body.get("front") == preview_front and body.get("back") == preview_back, "")

        # 13. 两阶段重写取消:preview→cancel,原卡内容/版本不变(PRD 重写失败/取消原卡不变)
        card_cancel = card_ids[2]
        before = by_id[card_cancel]
        r = client.create_rewrite_preview(c, card_cancel, step="rewrite-preview-cancel")
        check("第二张卡 rewrite-previews -> 201", r.status == 201, f"({r.status})")
        rewrite_id2 = client.body(r).get("rewrite_id")
        r = client.cancel_rewrite_preview(c, card_cancel, rewrite_id2, step="rewrite-cancel")
        check("取消预览 -> 204", r.status == 204, f"({r.status})")
        r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards-after-cancel")
        after_cancel = [it for it in client.body(r).get("items", []) if it.get("card_id") == card_cancel]
        check("取消后原卡内容/版本不变",
              bool(after_cancel)
              and after_cancel[0].get("front") == before.get("front")
              and after_cancel[0].get("back") == before.get("back")
              and after_cancel[0].get("version") == before.get("version"),
              "")

        # 14. 删除批次 10 秒撤销:删除 → 不可见 → pending 恢复 → undo → 恢复可见
        card_del = card_ids[3]
        r = client.delete_card(c, card_del, step="card-delete")
        body = client.body(r)
        check("DELETE /cards/{id} -> 200 删除批次",
              r.status == 200 and body.get("status") == "PENDING", f"({r.status}) {body.get('status')}")
        batch_id = body.get("delete_batch_id")
        check("删除批次含 undo_until", bool(body.get("undo_until")), "")
        check("批次含被删卡", card_del in (body.get("card_ids") or []), "")
        r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards-after-delete")
        gone = [it for it in client.body(r).get("items", []) if it.get("card_id") == card_del]
        check("删除后卡片不可见(可见谓词排除)", not gone, "")
        r = client.pending_deletion_batches(c, step="deletion-pending")
        pend = [b for b in client.body(r).get("items", [])
                if b.get("delete_batch_id") == batch_id]
        check("pending 恢复撤销批次(重启可见)", bool(pend), "")
        r = client.undo_deletion_batch(c, batch_id, step="deletion-undo")
        body = client.body(r)
        check("undo -> 200 UNDONE", r.status == 200 and body.get("status") == "UNDONE",
              f"({r.status}) {body.get('status')}")
        r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards-after-undo")
        back = [it for it in client.body(r).get("items", []) if it.get("card_id") == card_del]
        check("撤销后卡片恢复可见", bool(back), "")

        # 15. 真实统计:周目标 = 每日目标 × 7、时区 = 账号学习时区、本周复习计入
        r = c.request("GET", "/stats/dashboard", step="dashboard")
        body = client.body(r)
        check("GET /stats/dashboard -> 200", r.status == 200, f"({r.status})")
        check("看板周目标 = 每日目标 × 7(服务端派生)", body.get("weekly_goal") == goal * 7,
              f"goal={body.get('weekly_goal')}")
        check("看板时区 = 账号学习时区", body.get("timezone") == prefs.get("learning_timezone"),
              f"{body.get('timezone')}")
        check("看板周活动 7 桶", isinstance(body.get("weekly_activity"), list)
              and len(body.get("weekly_activity")) == 7,
              f"buckets={len(body.get('weekly_activity')) if isinstance(body.get('weekly_activity'), list) else '?'}")
        check("看板本周复习 >= 1(本次评级计入)", body.get("weekly_total", 0) >= 1,
              f"weekly={body.get('weekly_total')}")
    finally:
        # 异常路径兜底清理(含 SystemExit):偏好还原原值;牌组/任务清理;注销会话
        if prefs_mutated:
            r = c.request("PATCH", "/preferences",
                          body={"daily_learning_goal": orig_goal,
                                "current_project_id": orig_current_project},
                          idempotent=True, step="prefs-restore")
            check("恢复偏好(每日目标/当前项目)", r.status == 200, f"({r.status})")
        if not keep:
            if deck_id is not None:
                r = c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup")
                check("清理项目牌组", r.status in (200, 204), f"({r.status})")
            if task_id is not None:
                r = client.delete_task(c, task_id, step="task-delete")
                check("清理终态任务", r.status == 204, f"({r.status})")
        else:
            print("    [keep] 保留测试牌组与任务,不清理")
        r = c.logout()
        check("logout -> 204", r.status == 204, f"({r.status})")

    if created:
        record("local_test_users_created", created)
    return summary()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--environment", default="local", choices=list(environments.ENVIRONMENTS))
    ap.add_argument("--run-id", default=None, help="runner 注入(牌组命名);直跑时自动生成")
    ap.add_argument("--keep", action="store_true", help="不清理创建的牌组与任务")
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
               keep=args.keep)


if __name__ == "__main__":
    sys.exit(main())
