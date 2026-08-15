"""scenarios.flow v25 场景逻辑层单元测试:无网络,StubClient 录制调用序列与报告计数。

RED 清单覆盖:
- 精确请求顺序:V2.5 主链路/恢复链路的有序子序列断言;
- 幂等键:全部写操作携带 Idempotency-Key(经 5 参 handler 捕获),评级重放同键;
- 清理行为:生成失败路径 finally 仍还原偏好、清牌组/任务、注销(异常路径兜底);
- 失败计数:场景失败非零退出(无 fake PASS),runner 对失败数非零返回 1;
- 安全日志:console 输出不含 API Key/密码/token 与卡片内容(红线 4 + 卡片内容红线);
- 套件区分:zero-LLM recovery(0 次)与 cost-confirmed generation(core_flow 61 次,
  超阈值必须 --confirm-cost)独立注册,quick/full/live 语义不变。
"""
import contextlib
import copy
import io
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.client import Response
from scenarios.flow import v25_core_flow, v25_recovery
from runner import suites
from tests import stub

EMAIL = "tester@local.test"  # 占位凭据(真实凭据只走环境变量)
PASSWORD = "pw-123456"
API_KEY = "sk-test-secret"
TOKEN = "tok-u-tester"
RUN_ID = "3f2a9c81d4e54b679c1d2e3f4a5b6c7d"


def _method_paths(calls: list) -> list[tuple[str, str]]:
    """调用序列的 (method, path) 视图(register 等账号调用的第二元为用户名)。"""
    return [(m, str(p)) for m, p, _ in calls]


def _subseq(haystack: list, needle: list) -> bool:
    """有序子序列匹配(消费迭代器,允许中间穿插 GET/恢复步骤;输入为 method-path 视图)。"""
    it = iter(haystack)
    return all(x in it for x in needle)


def _paths(calls: list) -> list[str]:
    return [p for _, p, _ in calls]


# ---------------------------------------------------------------- v25_core_flow


def _task_body(**overrides) -> dict:
    base = {
        "task_id": "task-1", "project_id": "proj-1", "file_id": "pdf-1", "deck_id": "deck-1",
        "retry_of_task_id": None, "status": "DRAFT", "internal_stage": None,
        "selected_chapters": [{"chapter_id": "ch-1", "name": "第一章", "start_page": 1,
                               "end_page": 10}],
        "generation_config": {"coverage_mode": "BALANCED", "difficulty_ratio": {
            "basic": 40, "understanding": 40, "deep_question": 20}},
        "sample_cards": None, "sample_config_hash": None, "sample_confirmed_at": None,
        "generated_card_count": 0, "resumable": False, "failure_stage": None,
        "error_code": None, "created_at": "2026-08-16T00:00:00Z", "updated_at": "2026-08-16T00:00:00Z",
    }
    base.update(overrides)
    return base


def _card(card_id: str, position: int, **overrides) -> dict:
    base = {
        "card_id": card_id, "deck_id": "deck-1", "source": "GENERATED", "position": position,
        "front": f"front-{position}", "back": f"back-{position}", "card_type": "QUESTION",
        "version": "v1", "source_task_id": "task-1", "chapter_id": "ch-1",
        "created_at": "2026-08-16T00:00:00Z", "updated_at": "2026-08-16T00:00:00Z",
    }
    base.update(overrides)
    return base


def _core_handler(*, fail_generation: bool = False, no_project: bool = False):
    """5 参 handler(method, path, body, auth, idempotency_key):状态化 stub 后端。

    记录 st["write_keys"] = [(path, key)](幂等键断言)与 st["review_keys"]。
    卡片内容(重写预览文本)仅存在于响应,场景与报告不得输出。
    """
    st = {
        "me": {"user_id": "u-tester", "username": "tester", "email": EMAIL,
               "avatar_key": "mood_01", "created_at": "2026-08-14T00:00:00Z"},
        "goal": 50,
        "current_project": None,
        "samples": False,
        "started": False,
        "completed": False,
        "failed": fail_generation,
        "cards": [_card(f"card-{i}", i) for i in range(1, 7)],
        "rewrite_versions": {},
        "pending_delete": set(),
        "batches": {},
        "reviews": {},       # idempotency_key -> 首次评级响应(幂等重放)
        "rewrite_seq": 0,
    }

    def handler(method, path, body, auth, key):
        if path == "/auth/register":
            return Response(201, stub.session_body("u-tester", "tester"))
        if path == "/auth/login":
            return Response(200, stub.session_body("u-tester", "tester"))
        if path == "/auth/logout":
            return Response(204, None)
        if path == "/api-key":
            return Response(200, {"status": "AVAILABLE", "masked_key": "sk-****abcd",
                                  "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/api-key/status":
            return Response(200, {"status": "AVAILABLE", "masked_key": "sk-****abcd",
                                  "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/auth/me" and method == "GET":
            return Response(200, {"user": dict(st["me"])})
        if path == "/auth/me" and method == "PATCH":
            st["me"]["username"] = body["username"]
            st["me"]["avatar_key"] = body["avatar_key"]
            return Response(200, {"user": dict(st["me"])})
        if path == "/preferences" and method == "GET":
            return Response(200, {
                "default_coverage_mode": "BALANCED",
                "default_difficulty_ratio": {"basic": 40, "understanding": 40,
                                             "deep_question": 20},
                "daily_learning_goal": st["goal"], "learning_timezone": "Asia/Shanghai",
                "current_project_id": st["current_project"],
                "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/preferences" and method == "PATCH":
            if body.get("daily_learning_goal") is not None:
                st["goal"] = body["daily_learning_goal"]
            if "current_project_id" in body:
                st["current_project"] = body["current_project_id"]
            return Response(200, {
                "default_coverage_mode": "BALANCED",
                "default_difficulty_ratio": {"basic": 40, "understanding": 40,
                                             "deep_question": 20},
                "daily_learning_goal": st["goal"], "learning_timezone": "Asia/Shanghai",
                "current_project_id": st["current_project"],
                "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/projects" and method == "GET":
            if no_project:
                return Response(200, {"items": []})
            return Response(200, {"items": [
                {"project_id": "proj-1", "name": "样书项目", "status": "READY",
                 "file": {"file_id": "pdf-1"}, "chapter_count": 2, "deck_count": 0,
                 "task_count": 0, "created_at": "2026-08-15T00:00:00Z",
                 "updated_at": "2026-08-15T00:00:00Z", "version": "v1"}]})
        if path == "/projects/proj-1" and method == "GET":
            return Response(200, {
                "project_id": "proj-1", "name": "样书项目", "status": "READY",
                "file": {"file_id": "pdf-1", "filename": "sample.pdf", "size_bytes": 1000,
                         "status": "PARSED", "chapters": [
                             {"chapter_id": "ch-1", "name": "第一章", "start_page": 1,
                              "end_page": 10},
                             {"chapter_id": "ch-2", "name": "第二章", "start_page": 11,
                              "end_page": 20}]},
                "chapter_count": 2, "deck_count": 0, "task_count": 0,
                "created_at": "2026-08-15T00:00:00Z", "updated_at": "2026-08-15T00:00:00Z",
                "version": "v1"})
        if path == "/projects/proj-1" and method == "PATCH":
            return Response(200, {"project_id": "proj-1", "name": body["name"],
                                  "status": "READY", "chapter_count": 2})
        if path == "/projects/proj-1/chapters/ch-1" and method == "PATCH":
            return Response(200, {"chapter_id": "ch-1", "name": body["name"],
                                  "start_page": 1, "end_page": 10})
        if path == "/projects/proj-1/study-settings" and method == "GET":
            return Response(200, {"selected_new_card_chapter_ids": [], "include_unassigned": False,
                                  "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/projects/proj-1/study-settings" and method == "PATCH":
            return Response(200, {"selected_new_card_chapter_ids":
                                  body.get("selected_new_card_chapter_ids") or [],
                                  "include_unassigned":
                                  bool(body.get("include_unassigned", False)),
                                  "updated_at": "2026-08-16T00:00:00Z"})
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-1", "name": body["name"], "source": "MANUAL",
                                  "project_id": body.get("project_id"), "card_count": 0,
                                  "due_count": 0, "mastered_card_count": 0, "review_count": 0,
                                  "mastery_ratio": 0.0, "created_at": "2026-08-16T00:00:00Z",
                                  "updated_at": "2026-08-16T00:00:00Z", "version": "v1"})
        if path == "/projects/proj-1/tasks" and method == "POST":
            return Response(201, _task_body())
        if path == "/tasks/task-1" and method == "GET":
            if st["started"] and st["failed"]:
                # 生成阶段失败:样卡阶段正常,start 后 worker 失败
                return Response(200, _task_body(status="FAILED", failure_stage="GENERATING",
                                                error_code="GENERATION_FAILED"))
            if st["completed"]:
                return Response(200, _task_body(status="COMPLETED", generated_card_count=6,
                                                ended_at="2026-08-16T00:05:00Z"))
            if st["started"]:
                return Response(200, _task_body(status="GENERATING", internal_stage="PLANNING"))
            if st["samples"]:
                return Response(200, _task_body(
                    status="AWAITING_SAMPLE_CONFIRMATION",
                    sample_cards=[{"card_id": "s-1", "front": "样卡", "back": "答案",
                                   "card_type": "QUESTION"}],
                    sample_config_hash="hash-1"))
            return Response(200, _task_body())
        if path == "/tasks/task-1/samples" and method == "POST":
            st["samples"] = True
            return Response(200, _task_body(status="SAMPLE_GENERATING"))
        if path == "/tasks/task-1/start" and method == "POST":
            st["started"] = True
            st["completed"] = not st["failed"]
            return Response(200, _task_body(status="GENERATING", internal_stage="PLANNING"))
        if path == "/tasks/task-1/batches":
            return Response(200, {"items": [
                {"batch_id": "b-1", "task_id": "task-1", "batch_index": 0,
                 "status": "SUCCEEDED", "retry_count": 0, "cache_hit_tokens": 800,
                 "cache_miss_tokens": 400, "output_tokens": 100, "cost_estimate": 0.0048},
                {"batch_id": "b-2", "task_id": "task-1", "batch_index": 1,
                 "status": "SUCCEEDED", "retry_count": 0, "cache_hit_tokens": 900,
                 "cache_miss_tokens": 300, "output_tokens": 120, "cost_estimate": 0.0054},
            ]})
        if path == "/decks/deck-1/cards":
            # 深拷贝:响应不得与 stub 内部状态共享引用(否则场景快照被后续变更污染)
            return Response(200, {"items": [copy.deepcopy(c) for c in st["cards"]
                                            if c["card_id"] not in st["pending_delete"]]})
        if path == "/review-events" and method == "POST":
            if key in st["reviews"]:
                return Response(200, st["reviews"][key])
            resp = {"review_state": {"review_state_id": "rs-1", "card_id": body["card_id"],
                                     "state": "LEARNING", "stability": 2.5, "difficulty": 5.0,
                                     "due": "2026-08-16T00:10:00Z", "reps": 1, "lapses": 0,
                                     "updated_at": "2026-08-16T00:00:00Z"},
                    "study_date": "2026-08-16"}
            st["reviews"][key] = resp
            return Response(200, resp)
        if path == "/study/today":
            return Response(200, {
                "timezone": "Asia/Shanghai", "study_date": "2026-08-16",
                "current_project": {"project_id": st["current_project"], "name": "样书项目"}
                if st["current_project"] else None,
                "daily_goal": st["goal"], "today_completed_count": 1, "due_count": 0,
                "main_plan_remaining": st["goal"], "backlog_count": 0, "cards": []})
        if path == "/cards/card-2/rewrite-previews" and method == "POST":
            st["rewrite_seq"] += 1
            st["rewrite_versions"]["card-2"] = "v2"
            return Response(201, {"rewrite_id": "rew-1", "card_id": "card-2",
                                  "base_card_version": "v1", "front": "重写后正面",
                                  "back": "重写后背面", "card_type": "QUESTION",
                                  "target_difficulty": "BASIC", "status": "PENDING",
                                  "expires_at": "2026-08-17T00:00:00Z",
                                  "created_at": "2026-08-16T00:00:00Z"})
        if path == "/cards/card-3/rewrite-previews" and method == "POST":
            st["rewrite_seq"] += 1
            return Response(201, {"rewrite_id": "rew-2", "card_id": "card-3",
                                  "base_card_version": "v1", "front": "取消用正面",
                                  "back": "取消用背面", "card_type": "QUESTION",
                                  "target_difficulty": "BASIC", "status": "PENDING",
                                  "expires_at": "2026-08-17T00:00:00Z",
                                  "created_at": "2026-08-16T00:00:00Z"})
        if path == "/cards/card-2/rewrite-previews/rew-1/apply" and method == "POST":
            for c in st["cards"]:
                if c["card_id"] == "card-2":
                    c.update({"version": "v2", "front": "重写后正面", "back": "重写后背面"})
            return Response(200, _card("card-2", 2, version="v2", front="重写后正面",
                                       back="重写后背面", source_task_id="task-1"))
        if path == "/cards/card-3/rewrite-previews/rew-2" and method == "DELETE":
            return Response(204, None)
        if path == "/cards/card-4" and method == "DELETE":
            st["pending_delete"].add("card-4")
            st["batches"]["del-1"] = {"delete_batch_id": "del-1", "card_ids": ["card-4"],
                                      "undo_until": "2026-08-16T00:00:10Z",
                                      "status": "PENDING", "created_at": "2026-08-16T00:00:00Z",
                                      "updated_at": "2026-08-16T00:00:00Z"}
            return Response(200, st["batches"]["del-1"])
        if path == "/card-deletion-batches/pending":
            return Response(200, {"items": list(st["batches"].values())})
        if path == "/card-deletion-batches/del-1/undo" and method == "POST":
            st["batches"]["del-1"]["status"] = "UNDONE"
            st["pending_delete"].discard("card-4")
            return Response(200, st["batches"]["del-1"])
        if path == "/stats/dashboard":
            return Response(200, {
                "period": {"start": "2026-08-10T00:00:00Z", "end": "2026-08-16T00:00:00Z",
                           "week_ordinal": 33},
                "timezone": "Asia/Shanghai", "weekly_activity": [0, 0, 0, 0, 0, 0, 1],
                "weekly_total": 1, "weekly_completed_count": 1, "week_change_rate": None,
                "weekly_goal": st["goal"] * 7, "weekly_goal_progress": 0.05,
                "updated_at": "2026-08-16T00:00:00Z", "recall_accuracy": 1.0,
                "first_answer_accuracy": 1.0, "retention_rate": None, "streak_days": 1,
                "mastered_card_count": 0, "has_data": True})
        if path == "/decks/deck-1" and method == "DELETE":
            return Response(204, None)
        if path == "/tasks/task-1" and method == "DELETE":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler, st


def _run_core(handler):
    """run core_flow 并挂 request 探针(记录 idempotent/idempotency_key 调用视图)。"""
    c = stub.StubClient(handler)
    records: list[tuple[str, str, bool, str | None]] = []
    orig_request = c.request

    def spy(method, path, *, body=None, idempotent=False, idempotency_key=None,
            retry=True, step=""):
        records.append((method, path, idempotent, idempotency_key))
        return orig_request(method, path, body=body, idempotent=idempotent,
                            idempotency_key=idempotency_key, retry=retry, step=step)

    c.request = spy  # type: ignore[method-assign]
    buf = io.StringIO()
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(buf):
        failed = v25_core_flow.run(
            c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
            api_key=API_KEY, run_id=RUN_ID, keep=False)
    return c, buf.getvalue(), failed, records


class V25CoreFlowScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        # SystemExit 路径跳过 summary() 复位,report 全局 STEPS/META 会跨测试累积
        from shanka.report import META, STEPS
        STEPS.clear()
        META.clear()

    def test_run_local_full_flow(self) -> None:
        """主链路全绿:请求顺序子序列、幂等键纪律、评级重放同键、统计跨断言、安全日志。"""
        handler, st = _core_handler()
        c, out, failed, request_spy = _run_core(handler)
        self.assertEqual(failed, 0)
        calls = c.calls

        # 精确请求顺序:V2.5 主链路有序子序列(允许轮询 GET 与恢复步骤穿插)
        chain = [
            ("register", "tester"),
            ("PUT", "/api-key"),
            ("GET", "/api-key/status"),
            ("GET", "/auth/me"),
            ("PATCH", "/auth/me"),
            ("PATCH", "/auth/me"),                      # 改后立即恢复
            ("GET", "/preferences"),
            ("GET", "/projects"),
            ("GET", "/projects/proj-1"),
            ("PATCH", "/preferences"),                  # 每日目标(保持至流程末)
            ("PATCH", "/projects/proj-1"),
            ("PATCH", "/projects/proj-1"),              # 重命名后立即恢复
            ("PATCH", "/projects/proj-1/chapters/ch-1"),
            ("PATCH", "/projects/proj-1/chapters/ch-1"),
            ("GET", "/projects/proj-1/study-settings"),
            ("PATCH", "/projects/proj-1/study-settings"),
            ("PATCH", "/projects/proj-1/study-settings"),
            ("PATCH", "/preferences"),                  # 当前项目
            ("POST", "/decks"),
            ("POST", "/projects/proj-1/tasks"),
            ("GET", "/tasks/task-1"),                   # DRAFT 自动保存可读
            ("POST", "/tasks/task-1/samples"),
            ("GET", "/tasks/task-1"),                   # 轮询至 AWAITING_SAMPLE_CONFIRMATION
            ("POST", "/tasks/task-1/start"),
            ("GET", "/tasks/task-1"),                   # 轮询至 COMPLETED
            ("GET", "/tasks/task-1/batches"),
            ("GET", "/decks/deck-1/cards"),
            ("POST", "/review-events"),
            ("POST", "/review-events"),                 # 同键幂等重放
            ("GET", "/study/today"),
            ("POST", "/cards/card-2/rewrite-previews"),
            ("POST", "/cards/card-2/rewrite-previews/rew-1/apply"),
            ("POST", "/cards/card-3/rewrite-previews"),
            ("DELETE", "/cards/card-3/rewrite-previews/rew-2"),
            ("GET", "/decks/deck-1/cards"),             # 取消后原卡不变验证
            ("DELETE", "/cards/card-4"),
            ("GET", "/decks/deck-1/cards"),             # 删除后不可见
            ("GET", "/card-deletion-batches/pending"),
            ("POST", "/card-deletion-batches/del-1/undo"),
            ("GET", "/decks/deck-1/cards"),             # 撤销后恢复可见
            ("GET", "/stats/dashboard"),
            ("PATCH", "/preferences"),                  # 结束恢复偏好
            ("DELETE", "/decks/deck-1"),
            ("DELETE", "/tasks/task-1"),
            ("logout", ""),
        ]
        self.assertTrue(_subseq(_method_paths(calls), chain), f"顺序失配:\n{_paths(calls)}")

        # 幂等键纪律:全部写操作以 idempotent=True 发出(StubClient 只透传显式键,
        # 自动生成的键在客户端侧——经 request 探针观测);评级重放为同显式键
        records = {p: (idem, key) for m, p, idem, key in request_spy}
        write_paths = (
            "/api-key", "/auth/me", "/preferences", "/projects/proj-1",
            "/projects/proj-1/chapters/ch-1", "/projects/proj-1/study-settings",
            "/decks", "/projects/proj-1/tasks", "/tasks/task-1/samples",
            "/tasks/task-1/start", "/review-events", "/cards/card-2/rewrite-previews",
            "/cards/card-2/rewrite-previews/rew-1/apply",
            "/cards/card-3/rewrite-previews", "/cards/card-3/rewrite-previews/rew-2",
            "/cards/card-4", "/card-deletion-batches/del-1/undo",
            "/decks/deck-1", "/tasks/task-1",
        )
        for p in write_paths:
            self.assertIn(p, records, f"写路径缺幂等键: {p}")
            self.assertTrue(records[p][0], f"写路径未幂等: {p}")
        # 评级重放:两次请求同显式键,重放返回首次结果(不重复计数)
        review_records = [(k) for m, p, idem, k in request_spy if p == "/review-events"]
        self.assertEqual(len(review_records), 2)
        self.assertEqual(review_records[0], review_records[1])
        self.assertIn("幂等重放返回首次结果(不重复计数)", out)

        # 统计跨断言:周目标 = 每日目标 × 7,看板时区 = 账号学习时区
        self.assertIn("[PASS] 看板周目标 = 每日目标 × 7(服务端派生)", out)
        self.assertIn("[PASS] 看板时区 = 账号学习时区", out)
        self.assertIn("[PASS] 今日计划: 每日目标 = 服务端偏好", out)
        self.assertIn("[PASS] 今日计划: 当前项目已设置", out)

        # 成本对账与报告字段(推导 59,重写预览 2 次另计 LLM_CALLS)
        self.assertIn("llm_budget_calls=59", out)
        self.assertIn("llm_attempts_actual=2", out)
        self.assertIn("llm_tokens_actual=2620", out)
        self.assertIn("llm_cost_actual=0.0102", out)
        self.assertIn("PLANNING/SCORING 尝试数无 HTTP 观测入口", out)

        # 安全日志:API Key/密码/token 与卡片内容均不出现在 console(红线 4 + 卡片内容)
        for secret in (API_KEY, PASSWORD, TOKEN, "front-1", "重写后正面", "重写后背面"):
            self.assertNotIn(secret, out, f"console 泄漏: {secret}")

    def test_run_generation_failure_cleanup_and_nonzero(self) -> None:
        """生成 FAILED:轮询硬失败(SystemExit 非零),finally 仍还原偏好/清资源/注销。"""
        handler, _ = _core_handler(fail_generation=True)
        c = stub.StubClient(handler)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                v25_core_flow.run(
                    c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
                    api_key=API_KEY, run_id="run-x", keep=False)
        self.assertNotEqual(ctx.exception.code, 0)  # 消息型 SystemExit:解释器退出码非 0
        self.assertIn("任务未达", str(ctx.exception.code))
        self.assertIn("status=FAILED", str(ctx.exception.code))
        self.assertIn("error_code=GENERATION_FAILED", str(ctx.exception.code))
        calls = c.calls
        # 异常路径兜底清理:偏好还原(原值)+ 牌组/任务删除 + 注销
        prefs_patch = [body for m, p, body in calls if m == "PATCH" and p == "/preferences"]
        self.assertEqual(prefs_patch[-1], {"daily_learning_goal": 50,
                                           "current_project_id": None})
        self.assertIn(("DELETE", "/decks/deck-1", None), calls)
        self.assertIn(("DELETE", "/tasks/task-1", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 1)
        # 无 fake PASS:失败路径的 PASS 步骤不含任务完成
        self.assertNotIn("[PASS] 任务终态 COMPLETED", buf.getvalue())

    def test_run_no_ready_project_hard_fail_before_mutation(self) -> None:
        """前置缺失(无 READY 项目):非零退出,且偏好未被改动(前置在变更之前)。"""
        handler, _ = _core_handler(no_project=True)
        c = stub.StubClient(handler)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                v25_core_flow.run(
                    c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
                    api_key=API_KEY, run_id="run-x", keep=False)
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("无 READY 项目", str(ctx.exception.code))
        # 前置失败发生在偏好变更之前:无 /preferences 写操作,me 变更已恢复原值
        self.assertNotIn(("PATCH", "/preferences", mock.ANY), c.calls)
        me_patches = [body for m, p, body in c.calls if m == "PATCH" and p == "/auth/me"]
        self.assertEqual(me_patches, [{"username": "tester-v25", "avatar_key": "mood_02"},
                                      {"username": "tester", "avatar_key": "mood_01"}])
        # finally 兜底:会话仍注销
        self.assertIn(("logout", "", None), c.calls)

    def test_llm_calls_declaration(self) -> None:
        """cost-confirmed generation 套件声明:推导 59 + 重写预览 2 = 61,超默认阈值。"""
        self.assertEqual(v25_core_flow.BUDGET_FIXTURE,
                         {"chapters": 2, "quantity_tendency": "BALANCED",
                          "generate": True, "planning_groups": 3})
        self.assertEqual(v25_core_flow.REWRITE_PREVIEW_CALLS, 2)
        self.assertEqual(v25_core_flow.LIVE_BUDGET.total_calls(), 59)
        self.assertEqual(v25_core_flow.LLM_CALLS, 61)
        self.assertTrue(v25_core_flow.cost.requires_confirm(v25_core_flow.LLM_CALLS))

    def test_scenario_never_prints_secret_even_on_partial_failure(self) -> None:
        """无 READY 项目失败路径:console 仍不含凭据。"""
        handler, _ = _core_handler(no_project=True)
        c = stub.StubClient(handler)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                v25_core_flow.run(
                    c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
                    api_key=API_KEY, run_id="run-x", keep=False)
        out = buf.getvalue() + err.getvalue()
        for secret in (API_KEY, PASSWORD):
            self.assertNotIn(secret, out)


# ---------------------------------------------------------------- v25_recovery


def _recovery_handler(*, failed_tasks: list[dict] | None = None):
    """5 参 handler:恢复链路 stub(任务/牌组状态机 + FAILED 任务前置)。"""
    if failed_tasks is None:
        failed_tasks = [{"task_id": "fail-1", "project_id": "proj-1", "file_id": "pdf-1",
                         "deck_id": "deck-fail", "status": "FAILED",
                         "generated_card_count": 0,
                         "created_at": "2026-08-10T00:00:00Z",
                         "updated_at": "2026-08-10T00:05:00Z"}]
    st = {
        "failed_tasks": list(failed_tasks),
        "own_abandoned": False,
        "own_task_deleted": False,
        "own_deck_deleted": False,
        "retried_deleted": False,
    }

    def handler(method, path, body, auth, key):
        if path == "/auth/register":
            return Response(409, {"error": {"code": "EMAIL_TAKEN", "message": "邮箱已注册",
                                            "localization_key": "email_taken"}})
        if path == "/auth/login":
            return Response(200, stub.session_body("u-tester", "tester"))
        if path == "/auth/logout":
            return Response(204, None)
        if path == "/tasks?status=FAILED":
            return Response(200, {"items": st["failed_tasks"]})
        if path == "/projects" and method == "GET":
            return Response(200, {"items": [
                {"project_id": "proj-1", "name": "样书项目", "status": "READY",
                 "file": {"file_id": "pdf-1"}, "chapter_count": 1, "deck_count": 0,
                 "task_count": 0, "created_at": "2026-08-15T00:00:00Z",
                 "updated_at": "2026-08-15T00:00:00Z", "version": "v1"}]})
        if path == "/projects/proj-1" and method == "GET":
            return Response(200, {
                "project_id": "proj-1", "name": "样书项目", "status": "READY",
                "file": {"file_id": "pdf-1", "filename": "sample.pdf", "size_bytes": 1000,
                         "status": "PARSED", "chapters": [
                             {"chapter_id": "ch-1", "name": "第一章", "start_page": 1,
                              "end_page": 10}]},
                "chapter_count": 1, "deck_count": 0, "task_count": 0,
                "created_at": "2026-08-15T00:00:00Z", "updated_at": "2026-08-15T00:00:00Z",
                "version": "v1"})
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-2", "name": body["name"], "source": "MANUAL",
                                  "project_id": body.get("project_id"), "card_count": 0,
                                  "due_count": 0, "mastered_card_count": 0, "review_count": 0,
                                  "mastery_ratio": 0.0, "created_at": "2026-08-16T00:00:00Z",
                                  "updated_at": "2026-08-16T00:00:00Z", "version": "v1"})
        if path == "/projects/proj-1/tasks" and method == "POST":
            return Response(201, _task_body(task_id="task-2", deck_id="deck-2"))
        if path == "/tasks/task-2" and method == "GET":
            if st["own_task_deleted"]:
                return Response(404, {"error": {"code": "TASK_NOT_FOUND", "message": "任务不存在",
                                                "localization_key": "task_not_found"}})
            if st["own_abandoned"]:
                return Response(200, _task_body(task_id="task-2", deck_id="deck-2",
                                                status="ABANDONED",
                                                ended_at="2026-08-16T00:00:00Z"))
            return Response(200, _task_body(task_id="task-2", deck_id="deck-2"))
        if path == "/tasks/task-2" and method == "DELETE":
            st["own_task_deleted"] = True
            return Response(204, None)
        if path == "/tasks/task-2/retry" and method == "POST":
            return Response(409, {"error": {"code": "TASK_STATE_CONFLICT",
                                            "message": "仅失败任务可重试",
                                            "localization_key": "task_state_conflict"}})
        if path == "/tasks/task-2/abandon" and method == "POST":
            st["own_abandoned"] = True
            return Response(200, _task_body(task_id="task-2", deck_id="deck-2",
                                            status="ABANDONED",
                                            ended_at="2026-08-16T00:00:00Z"))
        if path == "/decks/deck-2" and method == "GET":
            return Response(200, {"deck_id": "deck-2", "name": "v25-recovery", "source": "MANUAL",
                                  "project_id": "proj-1", "card_count": 0})
        if path == "/decks/deck-2" and method == "DELETE":
            st["own_deck_deleted"] = True
            return Response(204, None)
        if path == "/decks/deck-2/cards":
            return Response(200, {"items": [{"card_id": "x-1", "deck_id": "deck-2",
                                             "source": "MANUAL", "position": 1,
                                             "front": "手动卡", "back": "手动答案",
                                             "card_type": "QUESTION", "version": "v1",
                                             "source_task_id": None}]})
        if path == "/tasks/fail-1/retry" and method == "POST":
            return Response(201, _task_body(task_id="task-3", deck_id="deck-fail",
                                            retry_of_task_id="fail-1", project_id="proj-1",
                                            file_id="pdf-1"))
        if path == "/tasks/fail-1" and method == "GET":
            return Response(200, _task_body(task_id="fail-1", deck_id="deck-fail",
                                            project_id="proj-1", file_id="pdf-1",
                                            status="FAILED", error_code="GENERATION_FAILED",
                                            failure_stage="GENERATING"))
        if path == "/tasks/task-3" and method == "GET":
            return Response(200, _task_body(task_id="task-3", deck_id="deck-fail",
                                            retry_of_task_id="fail-1", project_id="proj-1",
                                            file_id="pdf-1"))
        if path == "/tasks/task-3/abandon" and method == "POST":
            return Response(200, _task_body(task_id="task-3", deck_id="deck-fail",
                                            retry_of_task_id="fail-1", status="ABANDONED",
                                            ended_at="2026-08-16T00:00:00Z"))
        if path == "/tasks/task-3" and method == "DELETE":
            st["retried_deleted"] = True
            return Response(204, None)
        if path == "/decks/deck-fail/cards":
            return Response(200, {"items": [{"card_id": "y-1", "deck_id": "deck-fail",
                                             "source": "GENERATED", "position": 1,
                                             "front": "他任务卡", "back": "答案",
                                             "card_type": "QUESTION", "version": "v1",
                                             "source_task_id": "other-task"}]})
        return Response(200, {"status": "ok"})

    return handler, st


def _run_recovery(handler):
    c = stub.StubClient(handler)
    c2 = stub.StubClient(handler)
    buf = io.StringIO()
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(buf):
        failed = v25_recovery.run(
            c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
            run_id=RUN_ID, client_factory=lambda: c2)
    return c, c2, buf.getvalue(), failed


class V25RecoveryScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        from shanka.report import META, STEPS
        STEPS.clear()
        META.clear()

    def test_run_recovery_full_flow(self) -> None:
        """恢复链路全绿:设备无关读取、重登录不取消任务、retry 负向/正向、零部分可见。"""
        handler, st = _recovery_handler()
        c, c2, out, failed = _run_recovery(handler)
        self.assertEqual(failed, 0)
        calls = c.calls

        # 精确请求顺序:前置 FAILED 扫描 → 自有 DRAFT → 双客户端读取 → 重登存活 →
        # retry 拒绝(负向)→ abandon → 零部分可见 → 清理 → retry 正向 → 清理
        chain = [
            ("GET", "/tasks?status=FAILED"),            # 前置:可重试失败任务
            ("GET", "/projects"),
            ("GET", "/projects/proj-1"),
            ("POST", "/decks"),
            ("POST", "/projects/proj-1/tasks"),
            ("GET", "/tasks/task-2"),
            ("logout", ""),                             # 主客户端退出
            ("GET", "/tasks/task-2"),                   # 重登录后任务仍 DRAFT
            ("POST", "/tasks/task-2/retry"),            # 负向:DRAFT retry 拒绝
            ("POST", "/tasks/task-2/abandon"),
            ("GET", "/tasks/task-2"),
            ("GET", "/decks/deck-2/cards"),             # 零部分可见(自有)
            ("DELETE", "/tasks/task-2"),
            ("GET", "/tasks/task-2"),                   # 删除后 404
            ("DELETE", "/decks/deck-2"),
            ("POST", "/tasks/fail-1/retry"),            # 正向:FAILED retry 关联新任务
            ("GET", "/tasks/task-3"),
            ("GET", "/tasks/fail-1"),                   # 原失败任务零卡片
            ("GET", "/decks/deck-fail/cards"),          # 失败任务无已发布卡
            ("POST", "/tasks/task-3/abandon"),
            ("DELETE", "/tasks/task-3"),
            ("logout", ""),
        ]
        self.assertTrue(_subseq(_method_paths(calls), chain), f"顺序失配:\n{_paths(calls)}")

        # 第二客户端(设备无关):独立实例 register(409 回落)+login 后读取任务/牌组
        c2_flow = _method_paths(c2.calls)
        self.assertEqual(c2_flow,
                         [("register", "tester"), ("login", "tester@local.test"),
                          ("set_token", "tok-u-tester"),
                          ("GET", "/tasks/task-2"), ("GET", "/decks/deck-2"),
                          ("logout", "")])

        # 重登录后任务未被取消(仍 DRAFT,subsequence 已断言在 logout 之后)
        self.assertIn("[PASS] 重登录后任务仍 DRAFT(未取消)", out)
        self.assertIn("[PASS] 新客户端读取任务 -> 200 DRAFT(设备无关)", out)

        # retry 负向控制:409 TASK_STATE_CONFLICT
        self.assertIn("[PASS] DRAFT 任务 retry -> 409(仅失败可重试)", out)
        self.assertIn("[PASS] 错误码 TASK_STATE_CONFLICT", out)

        # retry 正向:retry_of_task_id 关联 + 复制项目/PDF/牌组 + 新任务可读
        self.assertIn("[PASS] FAILED 任务 retry -> 201 新任务", out)
        self.assertIn("[PASS] retry 关联 retry_of_task_id = 原任务", out)
        self.assertIn("[PASS] retry 复制项目/PDF/牌组", out)

        # 零部分可见:失败任务 generated_card_count=0 + 牌组无该任务卡片
        self.assertIn("[PASS] 失败任务 generated_card_count == 0", out)
        self.assertIn("[PASS] 零部分可见: 失败任务无已发布卡", out)
        self.assertIn("[PASS] 零部分可见: 牌组无本任务卡片", out)

        # 安全日志:密码/token 不出现在 console
        for secret in (PASSWORD, TOKEN, "pw-"):
            self.assertNotIn(secret, out, f"console 泄漏: {secret}")
        # 无临时账号创建(409 回落 login):不计数
        self.assertNotIn("local_test_users_created", out)

    def test_run_no_failed_task_hard_fail(self) -> None:
        """前置缺失(无可重试 FAILED 任务):非零退出,不创建任何自有资源。"""
        handler, _ = _recovery_handler(failed_tasks=[])
        c = stub.StubClient(handler)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                v25_recovery.run(
                    c, environment="local", username="tester", email=EMAIL, password=PASSWORD,
                    run_id="run-x")
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("无可重试 FAILED 任务", str(ctx.exception.code))
        self.assertNotIn(("POST", "/decks", mock.ANY), c.calls)
        self.assertIn(("logout", "", None), c.calls)  # 前置失败路径仍注销

    def test_recovery_zero_llm_declaration(self) -> None:
        """zero-LLM recovery 套件:LLM_CALLS=0,不触发 --confirm-cost(与生成套件区分)。"""
        self.assertEqual(v25_recovery.LLM_CALLS, 0)
        self.assertFalse(suites.cost.requires_confirm(v25_recovery.LLM_CALLS))


# ---------------------------------------------------------------- 套件注册与闸门


class V25SuitesTest(unittest.TestCase):
    def setUp(self) -> None:
        from shanka.report import META, STEPS
        STEPS.clear()
        META.clear()

    def test_v25_suite_membership_isolated(self) -> None:
        """v25 独立注册;quick/full/live 成员语义不变(不含 v25 场景)。"""
        self.assertEqual({s.NAME for s in suites.SUITES["v25"]},
                         {"v25_core_flow", "v25_recovery"})
        for name in ("quick", "full", "live"):
            members = {s.NAME for s in suites.SUITES[name]}
            self.assertNotIn("v25_core_flow", members)
            self.assertNotIn("v25_recovery", members)
        # 旧套件语义不弱化:构成保持不变
        self.assertEqual({s.NAME for s in suites.SUITES["quick"]}, {"auth", "api_smoke"})
        self.assertEqual({s.NAME for s in suites.SUITES["full"]},
                         {"auth", "isolation", "api_smoke"})
        self.assertEqual({s.NAME for s in suites.SUITES["live"]},
                         {"auth", "isolation", "api_smoke", "live_flow"})

    def test_v25_cost_gate_distinction(self) -> None:
        """zero-LLM 恢复套件不触发闸门;cost-confirmed 生成套件触发;v25 合计触发。"""
        self.assertFalse(suites.cost.requires_confirm(
            suites.cost.aggregate([v25_recovery])))
        self.assertTrue(suites.cost.requires_confirm(
            suites.cost.aggregate([v25_core_flow])))
        self.assertTrue(suites.cost.requires_confirm(suites.llm_total("v25")))
        self.assertEqual(suites.llm_total("v25"), v25_core_flow.LLM_CALLS)

    def test_v25_requires_confirm_cost_and_prod_gate(self) -> None:
        """--confirm-prod 与派生 --confirm-cost 拒绝语义对 v25 同样生效(不弱化)。"""
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_EMAIL": "u@local.test",
               "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            code = suites.main(["--environment", "local", "--suite", "v25"])
        self.assertEqual(code, 1)  # 无 --confirm-cost 拒绝
        with mock.patch.dict(os.environ, env):
            code = suites.main(["--environment", "prod", "--suite", "v25",
                                "--confirm-cost"])
        self.assertEqual(code, 1)  # prod 无 --confirm-prod 拒绝
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(suites.v25_core_flow, "main", return_value=0), \
                 mock.patch.object(suites.v25_recovery, "main", return_value=0), \
                 mock.patch("sys.stdout", io.StringIO()):
                code = suites.main(["--environment", "prod", "--suite", "v25",
                                    "--confirm-cost", "--confirm-prod"])
        self.assertEqual(code, 0)

    def test_v25_scenario_selector_runs_only_selected(self) -> None:
        """suite selector:--scenario 只跑指定场景(recovery 零 LLM 无需 --confirm-cost)。"""
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_EMAIL": "u@local.test",
               "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(suites.v25_core_flow, "main", return_value=0) as m1, \
                 mock.patch.object(suites.v25_recovery, "main", return_value=0) as m2, \
                 mock.patch("sys.stdout", io.StringIO()) as out:
                code = suites.main(["--environment", "local", "--suite", "v25",
                                    "--scenario", "v25_recovery"])
        self.assertEqual(code, 0)
        self.assertFalse(m1.called)
        self.assertTrue(m2.called)
        self.assertNotIn("成本闸门: --confirm-cost 已确认", out.getvalue())  # 0 LLM 不提示确认

    def test_v25_unknown_scenario_rejected(self) -> None:
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_EMAIL": "u@local.test",
               "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            with mock.patch("sys.stdout", io.StringIO()):
                code = suites.main(["--environment", "local", "--suite", "v25",
                                    "--scenario", "nope"])
        self.assertEqual(code, 1)

    def test_scenario_failure_propagates_nonzero_exit(self) -> None:
        """场景失败数非零 → runner 合计失败 → 退出码 1(无 fake PASS)。"""
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_EMAIL": "u@local.test",
               "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(suites.v25_core_flow, "main", return_value=2) as m1, \
                 mock.patch.object(suites.v25_recovery, "main", return_value=0) as m2, \
                 mock.patch("sys.stdout", io.StringIO()) as out:
                code = suites.main(["--environment", "local", "--suite", "v25",
                                    "--confirm-cost"])
        self.assertEqual(code, 1)
        self.assertTrue(m1.called and m2.called)
        self.assertIn("失败步骤 2", out.getvalue())


if __name__ == "__main__":
    unittest.main()
