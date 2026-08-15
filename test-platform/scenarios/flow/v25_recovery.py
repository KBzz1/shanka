"""V2.5 恢复与设备无关性场景(零 LLM):设备无关检索、退出/重登不取消任务、失败任务重试、
零部分可见。

覆盖 release-and-acceptance 第 3/4 节恢复语义在 HTTP 层的零 LLM 可断言部分:
- 设备无关检索:新客户端实例(模拟另一台设备/重装,无任何设备绑定)登录后,同一账号的
  任务与牌组直接可读(账号化:数据按 user 隔离,不按设备);
- 退出再登录不取消生成任务:DRAFT 自动保存任务(等价于生成期任务由服务端 worker 继续,
  不受会话撤销影响——零 LLM 下以持久化 DRAFT 为代理断言)跨 logout/relogin 存活;
- 失败任务重试:FAILED → POST /tasks/{id}/retry 创建关联新任务(retry_of_task_id 指向
  原任务,项目/PDF/牌组/配置复制);DRAFT 任务 retry 拒绝(409 TASK_STATE_CONFLICT,
  零 LLM 负向控制);
- 零部分可见:未完成任务 generated_card_count=0、其目标牌组无该任务卡片(STAGED 卡对
  任何用户侧查询不可见,任务整体成功前不发布)。

LLM_CALLS = 0(不触发 --confirm-cost,与 cost-confirmed 生成套件区分);不读 API Key。
前置条件(沿用 live_flow 复用既有数据约定):测试账号需已有 READY 项目(≥1 章节)
与至少一个可重试 FAILED 任务(deck/project/file 均存活;需先预置一次失败制卡记录,
如 API Key 失效触发)。v25 套件中 core_flow 先行保存 API Key,重试链路不额外校验。
运行方式(由 runner 调度或直接):
    python3 scenarios/flow/v25_recovery.py --base-url http://localhost:8000 [--environment local|prod] [--run-id UUID]
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Callable

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/flow),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/flow/v25_recovery.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka import account, environments, logging as shlogging
from shanka import client
from shanka.client import ShankaClient
from shanka.report import check, record, summary

NAME = "v25_recovery"
SUITE = "flow"
LLM_CALLS = 0

_RECOVERY_CONFIG = {
    "coverage_mode": "BALANCED",
    "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
}
_PRE_GENERATION_STATES = ("DRAFT", "SAMPLE_GENERATING", "AWAITING_SAMPLE_CONFIRMATION")


def _retryable_failed(tasks: list[dict]) -> list[dict]:
    """可重试失败任务:FAILED 且 项目/PDF/牌组 均存活(重试复制这三者,缺一不可)。"""
    return [
        t for t in tasks
        if t.get("status") == "FAILED"
        and t.get("project_id") and t.get("file_id") and t.get("deck_id")
    ]


def run(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    email: str,
    password: str,
    run_id: str,
    client_factory: Callable[[], ShankaClient] | None = None,
) -> int:
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id="")
    if client_factory is None:
        # 新客户端模拟另一设备/重装:同 base_url 新实例(无任何设备绑定状态)
        client_factory = lambda: ShankaClient(c.base_url, pace=c.pace,  # type: ignore[attr-defined]
                                              timeout=getattr(c, "timeout", 30.0))

    # 0. 会话建立(local register/已存在回落 login;prod 只 login)
    session = account.bootstrap(c, environment=environment, username=username,
                                email=email, password=password)
    check("建立会话(register/login)", session is not None)
    if session is None:
        return summary()
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    created = 1 if session["created_local_user"] else 0

    # 1. 前置:可重试 FAILED 任务(失败重试链路的真实数据;缺失明确报错,不自动构造)
    r = c.request("GET", "/tasks?status=FAILED", step="failed-tasks")
    check("GET /tasks?status=FAILED -> 200", r.status == 200, f"({r.status})")
    candidates = _retryable_failed(client.body(r).get("items", []) or [])
    failed = candidates[0] if candidates else None
    check("存在可重试 FAILED 任务(前置)", failed is not None, f"failed={len(candidates)}")
    if failed is None:
        r = c.logout()
        check("logout(前置失败路径)", r.status == 204, f"({r.status})")
        raise SystemExit("当前用户无可重试 FAILED 任务:请先预置一次失败制卡记录"
                         "(如 API Key 失效触发)后重跑")

    # 2. 自有资源:READY 项目前置 + 项目牌组 + DRAFT 自动保存任务(零 LLM)
    r = c.request("GET", "/projects", step="project-list")
    check("GET /projects -> 200", r.status == 200, f"({r.status})")
    ready = [p for p in client.body(r).get("items", []) if p.get("status") == "READY"]
    check("存在 READY 项目(前置)", bool(ready), f"ready={len(ready)}")
    if not ready:
        r = c.logout()
        check("logout(前置失败路径)", r.status == 204, f"({r.status})")
        raise SystemExit("当前用户无 READY 项目:请先预置已解析确认的项目后重跑")
    project_id = ready[0]["project_id"]
    r = c.request("GET", f"/projects/{project_id}", step="project-detail")
    chapters = [ch for ch in (client.body(r).get("file") or {}).get("chapters") or []
                if isinstance(ch, dict)]
    check("项目章节非空(前置)", bool(chapters), f"chapters={len(chapters)}")
    if not chapters:
        r = c.logout()
        check("logout(前置失败路径)", r.status == 204, f"({r.status})")
        raise SystemExit(f"项目 {project_id[:8]} 无章节,无法建任务")

    deck_id = client.body(c.request("POST", "/decks",
                                    body={"name": f"v25-recovery-{run_id[:6]}",
                                          "project_id": project_id},
                                    idempotent=True, step="deck-create")).get("deck_id")
    check("POST /decks 建项目牌组 -> 201", isinstance(deck_id, str), f"deck={str(deck_id)[:8]}")
    if not isinstance(deck_id, str):
        raise SystemExit("建牌组响应缺 deck_id")

    r = client.create_task(c, project_id=project_id, deck_id=deck_id,
                           chapter_ids=[chapters[0]["chapter_id"]],
                           generation_config=_RECOVERY_CONFIG)
    body = client.body(r)
    check("POST /projects/{id}/tasks -> 201 DRAFT", r.status == 201 and body.get("status") == "DRAFT",
          f"({r.status}) {body.get('status')}")
    task_id = body.get("task_id")
    if not isinstance(task_id, str):
        raise SystemExit(f"任务创建响应缺 task_id: {body}")
    r = c.request("GET", f"/tasks/{task_id}", step="task-get")
    check("GET /tasks/{id} DRAFT 自动保存可读", client.body(r).get("status") == "DRAFT",
          f"status={client.body(r).get('status')}")

    # 3. 设备无关检索:新客户端实例(另一台设备/重装)登录后读取任务与牌组
    c2 = client_factory()
    session2 = account.bootstrap(c2, environment=environment, username=username,
                                 email=email, password=password)
    check("第二客户端(设备无关)会话建立", session2 is not None)
    if session2 is None:
        # 新客户端会话失败(如限流):自有资源清理后退出,不留下半成品
        client.abandon_task(c, task_id, step="task-abandon-fallback")
        client.delete_task(c, task_id, step="task-delete-fallback")
        c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup-fallback")
        r = c.logout()
        check("第二客户端会话失败: 清理自有资源后退出", r.status == 204, f"({r.status})")
        raise SystemExit("第二客户端会话建立失败(设备无关读取无法执行)")
    r = c2.request("GET", f"/tasks/{task_id}", step="task-read-other-client")
    check("新客户端读取任务 -> 200 DRAFT(设备无关)",
          r.status == 200 and client.body(r).get("status") == "DRAFT",
          f"({r.status}) {client.body(r).get('status')}")
    r = c2.request("GET", f"/decks/{deck_id}", step="deck-read-other-client")
    check("新客户端读取牌组 -> 200", r.status == 200, f"({r.status})")
    r = c2.logout()
    check("第二客户端 logout -> 204", r.status == 204, f"({r.status})")

    # 4. 退出再登录不取消生成任务(DRAFT 自动保存语义;生成期任务由服务端 worker 继续)
    r = c.logout()
    check("主客户端 logout -> 204", r.status == 204, f"({r.status})")
    session3 = account.bootstrap(c, environment=environment, username=username,
                                 email=email, password=password)
    check("重新登录", session3 is not None)
    if session3 is None:
        raise SystemExit("重新登录失败")
    shlogging.set_context(suite=SUITE, scenario=NAME, user_id=session["user_id"])
    r = c.request("GET", f"/tasks/{task_id}", step="task-after-relogin")
    check("重登录后任务仍 DRAFT(未取消)",
          r.status == 200 and client.body(r).get("status") == "DRAFT",
          f"status={client.body(r).get('status')}")

    # 5. retry 负向控制:非失败任务 retry 拒绝(409 TASK_STATE_CONFLICT,零 LLM)
    r = client.retry_task(c, task_id, step="task-retry-rejected")
    check("DRAFT 任务 retry -> 409(仅失败可重试)", r.status == 409, f"({r.status})")
    check("错误码 TASK_STATE_CONFLICT", client.error_code(r) == "TASK_STATE_CONFLICT",
          client.error_code(r))

    # 6. 放弃自有任务 + 零部分可见(未完成任务不发布任何卡片)
    r = client.abandon_task(c, task_id, step="task-abandon")
    body = client.body(r)
    check("abandon -> 200 ABANDONED", r.status == 200 and body.get("status") == "ABANDONED",
          f"({r.status}) {body.get('status')}")
    check("abandon 记录 ended_at", bool(body.get("ended_at")), "")
    r = c.request("GET", f"/tasks/{task_id}", step="task-after-abandon")
    body = client.body(r)
    check("终态 ABANDONED 可读", body.get("status") == "ABANDONED", f"status={body.get('status')}")
    check("零部分可见: generated_card_count == 0", body.get("generated_card_count") == 0,
          f"cards={body.get('generated_card_count')}")
    r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards-zero-partial")
    mine = [it for it in client.body(r).get("items", []) if it.get("source_task_id") == task_id]
    check("零部分可见: 牌组无本任务卡片", not mine, f"cards={len(mine)}")

    # 7. 清理自有资源(ABANDONED 终态删除 + 牌组)
    r = client.delete_task(c, task_id, step="task-delete")
    check("删除 ABANDONED 任务 -> 204", r.status == 204, f"({r.status})")
    r = c.request("GET", f"/tasks/{task_id}", step="task-after-delete")
    check("任务已删除(404)", r.status == 404, f"({r.status})")
    r = c.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup")
    check("清理自有牌组", r.status in (200, 204), f"({r.status})")

    # 8. 失败任务重试(正向):关联新任务 + 复制项目/PDF/牌组 + 可恢复状态
    orig_task_id = failed["task_id"]
    r = client.retry_task(c, orig_task_id, step="task-retry")
    body = client.body(r)
    check("FAILED 任务 retry -> 201 新任务", r.status == 201 and bool(body.get("task_id")),
          f"({r.status})")
    new_id = body.get("task_id")
    check("retry 关联 retry_of_task_id = 原任务", body.get("retry_of_task_id") == orig_task_id,
          f"retry_of={body.get('retry_of_task_id')}")
    check("retry 复制项目/PDF/牌组",
          body.get("project_id") == failed.get("project_id")
          and body.get("file_id") == failed.get("file_id")
          and body.get("deck_id") == failed.get("deck_id"),
          "")
    check("新任务可恢复状态(DRAFT/AWAITING_SAMPLE_CONFIRMATION)",
          body.get("status") in _PRE_GENERATION_STATES, f"status={body.get('status')}")
    r = c.request("GET", f"/tasks/{new_id}", step="task-retry-get")
    check("新任务 GET 可读", client.body(r).get("task_id") == new_id, "")

    # 9. 零部分可见(原失败任务):生成计数 0 + 其牌组无该任务已发布卡
    r = c.request("GET", f"/tasks/{orig_task_id}", step="task-failed-get")
    check("失败任务 generated_card_count == 0", client.body(r).get("generated_card_count") == 0,
          f"cards={client.body(r).get('generated_card_count')}")
    r = c.request("GET", f"/decks/{failed['deck_id']}/cards", step="deck-failed-cards")
    orig_cards = [it for it in client.body(r).get("items", [])
                  if it.get("source_task_id") == orig_task_id]
    check("零部分可见: 失败任务无已发布卡", not orig_cards, f"cards={len(orig_cards)}")

    # 10. 清理重试任务(abandon 未生成新任务 + 删除;原失败任务保留供追溯)
    r = client.abandon_task(c, new_id, step="task-retry-abandon")
    check("放弃重试任务 -> 200", r.status == 200, f"({r.status})")
    r = client.delete_task(c, new_id, step="task-retry-delete")
    check("删除重试任务 -> 204", r.status == 204, f"({r.status})")

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
    args = ap.parse_args(argv)
    try:
        username, email, password = environments.credentials()
    except environments.MissingCredentialsError as exc:
        print(f"拒绝执行: {exc}", file=sys.stderr)
        return 1
    c = ShankaClient(args.base_url)
    return run(c, environment=args.environment, username=username, email=email,
               password=password, run_id=args.run_id or str(uuid.uuid4()))


if __name__ == "__main__":
    sys.exit(main())
