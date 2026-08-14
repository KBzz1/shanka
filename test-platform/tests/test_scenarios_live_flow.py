"""scenarios.flow.live_flow 逻辑层单元测试:无网络,StubClient 录制调用序列与报告计数。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.client import Response
from scenarios.flow import live_flow
from tests import stub


def _handler(*, generate=True, prod=False, batches=None):
    state = {"polls": 0, "obs": False}

    def handler(method, path, body):
        if path == "/auth/register":
            state["obs"] = body["username"].startswith("t-")  # 临时观测账号
            return Response(201, stub.session_body(f"u-{body['username']}", body["username"]))
        if path == "/auth/login":
            return Response(200, stub.session_body(f"u-{body['username']}", body["username"]))
        if path == "/api-key":
            return Response(200, {"status": "ok"})
        if path == "/api-key/status":
            return Response(200, {"status": "AVAILABLE"})
        if path == "/pdfs":
            return Response(200, {"items": [{"file_id": "pdf-1", "status": "PARSED"}]})
        if path == "/pdfs/pdf-1":
            return Response(200, {"file_id": "pdf-1",
                                  "chapters": [{"chapter_id": "ch-1"}, {"chapter_id": "ch-2"}]})
        if path == "/samples":
            return Response(200, {"samples": [{"card_id": "s-1"}]})
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-1"})
        if path == "/tasks":
            return Response(201, {"task_id": "task-1"})
        if path == "/tasks/task-1":
            state["polls"] += 1
            if state["polls"] < 3:
                return Response(200, {"status": "GENERATING", "generated_card_count": 0})
            return Response(200, {"status": "COMPLETED", "generated_card_count": 3})
        if path == "/tasks/task-1/batches":
            if batches is not None:
                return Response(200, {"items": batches})
            return Response(200, {"items": [
                {"batch_id": "b-1", "batch_index": 0, "status": "SUCCEEDED", "retry_count": 0,
                 "cache_hit_tokens": 800, "cache_miss_tokens": 400, "output_tokens": 100,
                 "cost_estimate": 0.0048},
                {"batch_id": "b-2", "batch_index": 1, "status": "SUCCEEDED", "retry_count": 0,
                 "cache_hit_tokens": 900, "cache_miss_tokens": 300, "output_tokens": 120,
                 "cost_estimate": 0.0054},
            ]})
        if path == "/decks/deck-1/cards":
            return Response(200, {"items": [{"card_id": "card-1"}]})
        if path == "/review-events":
            return Response(200, {"state": "LEARNING", "due": "2026-08-15T00:00:00Z"})
        if path == "/stats/dashboard?timezone=Asia/Shanghai":
            return Response(200, {"weekly_total": 1, "mastered_card_count": 0})
        if path == "/observability/quality-summary":
            if state["obs"]:
                return Response(200, {"group_by": "model", "days": 30, "groups": []})
            return Response(200, {"group_by": "model", "days": 30,
                                  "groups": [{"key": "deepseek-chat", "card_count": 3}]})
        if path == "/decks/deck-1" and method == "DELETE":
            return Response(204, None)
        if path == "/decks/deck-1" and method == "GET":
            return Response(404, {"error": {"code": "NOT_FOUND"}})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


class LiveFlowScenarioTest(unittest.TestCase):
    def test_run_local_full_flow_with_observability_isolation(self) -> None:
        c = stub.StubClient(_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = live_flow.run(
                c, environment="local", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
                skip_generate=False, keep=False,
            )
        self.assertEqual(failed, 0)
        calls = c.calls
        # 会话:Bearer 全流程,local register 主账号 + 观测临时账号
        self.assertIn(("register", "tester", None), calls)
        self.assertIn(("register", "t-3f2a9c81d4-obs", None), calls)
        # 生成链路:api-key -> pdf -> samples -> task -> poll -> cards -> review -> dashboard
        paths = {p for _, p, _ in calls}
        for path in ("/api-key", "/pdfs", "/samples", "/tasks", "/decks/deck-1/cards",
                     "/review-events", "/stats/dashboard?timezone=Asia/Shanghai"):
            self.assertIn(path, paths)
        # 轮询至 COMPLETED(非终态轮询 2 次 + 终态 1 次)
        self.assertEqual(calls.count(("GET", "/tasks/task-1", None)), 3)
        # 成本对账:batches 观测 + 预算/实际报告字段(最坏预算 53,实际 2 次尝试/2620 token)
        self.assertIn(("GET", "/tasks/task-1/batches", None), calls)
        out = buf.getvalue()
        self.assertIn("对账: 生成尝试/批数在预算内", out)
        self.assertIn("llm_budget_calls=53", out)
        self.assertIn("llm_attempts_actual=2", out)
        self.assertIn("llm_tokens_actual=2620", out)
        self.assertIn("llm_cost_actual=0.0102", out)
        self.assertIn("PLANNING/SCORING 尝试数无 HTTP 观测入口", out)
        # quality-summary 按 user:主账号非空、观测临时账号为空(交叉断言)
        summaries = [p for m, p, _ in calls if p == "/observability/quality-summary"]
        self.assertEqual(len(summaries), 2)
        # 观测账号注销 + 切回主账号 + 主账号清理 + 注销
        self.assertIn(("set_token", "tok-u-tester", None), calls)
        self.assertIn(("DELETE", "/decks/deck-1", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 2)
        # 无法安全删除的 user 行(主账号 1 + 观测账号 1)计数报告
        self.assertIn("local_test_users_created=2", buf.getvalue())

    def test_run_reconciliation_over_budget_fails(self) -> None:
        """对账超预算(13 批 × 3 次尝试 = 39 > 36)→ FAIL 步骤,失败数 > 0。"""
        batches = [
            {"status": "SUCCEEDED", "retry_count": 2} for _ in range(13)
        ]
        c = stub.StubClient(_handler(batches=batches))
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = live_flow.run(
                c, environment="local", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="run-x", skip_generate=False, keep=False,
            )
        self.assertGreater(failed, 0)
        self.assertIn("[FAIL] 对账: 生成尝试/批数在预算内", buf.getvalue())
        self.assertIn("生成尝试 39/36", buf.getvalue())

    def test_run_skip_generate_ends_after_samples_with_logout(self) -> None:
        c = stub.StubClient(_handler())
        with redirect_stdout(io.StringIO()):
            failed = live_flow.run(
                c, environment="local", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="run-x", skip_generate=True, keep=False,
            )
        self.assertEqual(failed, 0)
        calls = c.calls
        paths = {p for _, p, _ in calls}
        self.assertIn("/samples", paths)
        self.assertNotIn("/tasks", paths)  # skip 不创建任务
        self.assertIn(("logout", "", None), calls)  # 会话仍注销

    def test_run_prod_no_temp_account(self) -> None:
        c = stub.StubClient(_handler(prod=True))
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = live_flow.run(
                c, environment="prod", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="run-x", skip_generate=True, keep=False,
            )
        self.assertEqual(failed, 0)
        self.assertNotIn(("register", "tester", None), c.calls)  # prod 禁自动注册
        self.assertFalse(any(u.startswith("t-") for op, u, _ in c.calls if op == "register"))
        self.assertNotIn("报告字段", buf.getvalue())

    def test_live_flow_obs_bootstrap_failure_warns(self) -> None:
        """观测账号 bootstrap 失败路径输出 WARN 且主 token 切回。"""
        base = _handler()

        def handler(method, path, body):
            if path == "/auth/register" and isinstance(body, dict) \
                    and str(body.get("username", "")).startswith("t-"):
                return Response(502, None)  # 观测临时账号注册失败(网关 502)
            return base(method, path, body)

        c = stub.StubClient(handler)
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = live_flow.run(
                c, environment="local", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="run-x", skip_generate=False, keep=False,
            )
        out = buf.getvalue()
        self.assertGreater(failed, 0)  # 「观测临时账号建立」步骤软 FAIL
        self.assertIn("[FAIL] 观测临时账号建立", out)
        self.assertIn("[warn]", out)
        self.assertIn("观测临时账号", out)
        self.assertIn("会话可能未撤销", out)
        # 切回主 token 后继续走主账号清理与注销(确定性收尾不变)
        calls = c.calls
        self.assertIn(("set_token", "tok-u-tester", None), calls)
        self.assertIn(("DELETE", "/decks/deck-1", None), calls)
        self.assertIn(("logout", "", None), calls)

    def test_run_no_session_early_return(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})),
        ))
        with redirect_stdout(io.StringIO()):
            failed = live_flow.run(
                c, environment="prod", username="tester", password="pw-123456",
                api_key="sk-test-secret", run_id="run-x", skip_generate=True, keep=False,
            )
        self.assertGreater(failed, 0)
        self.assertNotIn(("PUT", "/api-key", None), c.calls)


class LiveFlowHelpersTest(unittest.TestCase):
    def test_sample_cards_multi_field_compat(self) -> None:
        for payload in ({"samples": [1]}, {"cards": [1]}, {"items": [1]}, {"data": [1]}):
            self.assertEqual(len(live_flow._sample_cards(payload)), 1)
        self.assertEqual(live_flow._sample_cards({"other": "x"}), [])

    def test_body_safe(self) -> None:
        self.assertEqual(live_flow._body(Response(200, None)), {})
        self.assertEqual(live_flow._body(Response(200, {"a": 1})), {"a": 1})

    def test_env_path_is_file_relative(self) -> None:
        """_ENV_FILE 由 __file__ 推导,不含硬编码绝对路径,且指向仓库根。"""
        src = Path(live_flow.__file__).read_text()
        self.assertNotIn("/home/kbzz1", src)
        repo_root = Path(live_flow.__file__).resolve().parents[3]  # flow/ → scenarios/ → test-platform/ → 仓库根
        self.assertEqual(live_flow._ENV_FILE, repo_root / ".env")


if __name__ == "__main__":
    unittest.main()
