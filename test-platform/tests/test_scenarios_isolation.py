"""scenarios.isolation 逻辑层单元测试:无网络,StubClient 录制调用序列与报告计数。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.client import Response
from scenarios.isolation import isolation
from tests import stub


def _local_handler():
    state = {"delete_n": 0}

    def handler(method, path, body):
        if path == "/auth/register":
            username = body["username"]
            return Response(201, stub.session_body(f"u-{username}", username))
        if path == "/auth/login":
            username = body["username"]
            return Response(200, stub.session_body(f"u-{username}", username))
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-a", "name": body["name"]})
        if path == "/decks" and method == "GET":
            return Response(200, {"items": []})  # 第二用户列表(空,不含主账号牌组)
        if path == "/decks/deck-a" and method == "GET":
            return Response(404, {"error": {"code": "NOT_FOUND"}})
        if path == "/decks/deck-a" and method == "DELETE":
            state["delete_n"] += 1
            if state["delete_n"] == 1:
                return Response(404, {"error": {"code": "NOT_FOUND"}})  # 跨用户删除 404
            return Response(204, None)  # 主账号清理成功
        if path == "/observability/quality-summary":
            return Response(200, {"group_by": "model", "days": 30, "groups": []})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


def _prod_handler():
    def handler(method, path, body):
        if path == "/auth/login":
            return Response(200, stub.session_body("u-tester", "tester"))
        if path == "/decks" and method == "GET":
            return Response(200, {"items": []})
        if method == "GET" and path.startswith("/decks/"):
            return Response(404, {"error": {"code": "NOT_FOUND"}})
        if path == "/observability/quality-summary":
            return Response(200, {"group_by": "model", "days": 30, "groups": []})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


class IsolationScenarioTest(unittest.TestCase):
    def test_run_local_two_user_isolation(self) -> None:
        c = stub.StubClient(_local_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="local", username="tester", password="pw-123456",
                run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
            )
        self.assertEqual(failed, 0)
        calls = c.calls
        # 主账号 + run_id 命名临时账号
        self.assertEqual([u for op, u, _ in calls if op == "register"],
                         ["tester", "t-3f2a9c81d4-iso"])
        # 跨用户读/写 404 与列表隔离断言对应的调用
        self.assertIn(("GET", "/decks/deck-a", None), calls)
        self.assertIn(("DELETE", "/decks/deck-a", None), calls)
        # 主账号切回(临时账号注销后 set_token 主 token)+ 两 session 注销
        self.assertIn(("set_token", "tok-u-tester", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 2)
        # 无法安全删除的 user 行(主账号新建 1 + 临时账号 1)按 run 计数报告
        self.assertIn("local_test_users_created=2", buf.getvalue())

    def test_run_prod_read_only(self) -> None:
        c = stub.StubClient(_prod_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="prod", username="tester", password="pw-123456", run_id="run-1",
            )
        self.assertEqual(failed, 0)
        self.assertNotIn(("register", "tester", None), c.calls)  # prod 禁自动注册
        self.assertNotIn(("POST", "/decks", None), c.calls)  # 只读,无写操作
        self.assertEqual(c.calls.count(("logout", "", None)), 1)
        self.assertNotIn("报告字段", buf.getvalue())  # 无本地 user 行残留

    def test_run_deck_id_missing_cleans_up_by_prefix(self) -> None:
        """异常路径:POST /decks 201 但无 deck_id → 按前缀兜底清理残留牌组再注销。"""
        def handler(method, path, body):
            if path == "/auth/register":
                return Response(201, stub.session_body(f"u-{body['username']}", body["username"]))
            if path == "/decks" and method == "POST":
                return Response(201, {"ok": True})  # 缺 deck_id
            if path == "/decks" and method == "GET":
                return Response(200, {"items": [
                    {"deck_id": "deck-iso", "name": "iso-3f2a9c81"},
                    {"deck_id": "deck-other", "name": "其他牌组"},
                ]})
            if path == "/auth/logout":
                return Response(204, None)
            return Response(200, {"status": "ok"})

        c = stub.StubClient(handler)
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="local", username="tester", password="pw-123456",
                run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
            )
        self.assertGreater(failed, 0)
        calls = c.calls
        self.assertIn(("DELETE", "/decks/deck-iso", None), calls)  # 前缀命中才删
        self.assertNotIn(("DELETE", "/decks/deck-other", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 1)
        self.assertIn("[warn] 异常路径残留牌组已清理: iso-3f2a9c81", buf.getvalue())
        self.assertIn("local_test_users_created=1", buf.getvalue())

    def test_run_temp_account_bootstrap_failed_restores_main_and_warns(self) -> None:
        """异常路径:临时账号注册失败 → 切回主账号清理 + WARN(临时 session 可能未撤销)。"""
        def handler(method, path, body):
            if path == "/auth/register" and body["username"].startswith("t-"):
                return Response(500, {"error": {"code": "INTERNAL_ERROR"}})  # 临时账号失败
            if path == "/auth/register":
                return Response(201, stub.session_body("u-tester", "tester"))
            if path == "/decks" and method == "POST":
                return Response(201, {"deck_id": "deck-a"})
            if path == "/decks" and method == "GET":
                return Response(200, {"items": [{"deck_id": "deck-a", "name": "iso-3f2a9c81"}]})
            if path == "/decks/deck-a" and method == "DELETE":
                return Response(204, None)
            if path == "/auth/logout":
                return Response(204, None)
            return Response(200, {"status": "ok"})

        c = stub.StubClient(handler)
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="local", username="tester", password="pw-123456",
                run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
            )
        self.assertGreater(failed, 0)
        calls = c.calls
        # 切回主账号(确定性身份)→ 前缀清理 → 注销主账号 session
        self.assertIn(("set_token", "tok-u-tester", None), calls)
        self.assertIn(("DELETE", "/decks/deck-a", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 1)
        self.assertIn("会话可能未撤销", buf.getvalue())

    def test_run_no_session_early_return(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})),
        ))
        with redirect_stdout(io.StringIO()):
            failed = isolation.run(
                c, environment="prod", username="tester", password="pw-123456", run_id="run-1",
            )
        self.assertGreater(failed, 0)
        self.assertEqual(c.calls, [("login", "tester", None)])


if __name__ == "__main__":
    unittest.main()
