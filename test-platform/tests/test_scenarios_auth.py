"""scenarios.auth 逻辑层单元测试:无网络,StubClient 录制调用序列与报告计数。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.client import Response
from scenarios.auth import auth
from tests import stub

ME_BODY = {"user": {"user_id": "u-1", "username": "tester", "created_at": "2026-08-14T00:00:00Z"}}


def _local_flow_handler():
    me_count = {"n": 0}

    def handler(method, path, body):
        if path == "/decks":
            return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
        if path == "/auth/register":
            return Response(201, stub.session_body("u-1", "tester"))
        if path == "/auth/login":
            return Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})
        if path == "/auth/me":
            me_count["n"] += 1
            if me_count["n"] == 1:
                return Response(200, ME_BODY)
            return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


def _prod_flow_handler():
    me_count = {"n": 0}
    login_count = {"n": 0}

    def handler(method, path, body):
        if path == "/decks":
            return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
        if path == "/auth/login":
            login_count["n"] += 1
            if login_count["n"] == 1:
                return Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})  # 错误密码
            return Response(200, stub.session_body("u-1", "tester"))
        if path == "/auth/me":
            me_count["n"] += 1
            if me_count["n"] == 1:
                return Response(200, ME_BODY)
            return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


class AuthScenarioTest(unittest.TestCase):
    def test_run_local_full_sequence(self) -> None:
        """最小链路:401 -> 错误密码 -> register -> me -> logout -> me 401。"""
        c = stub.StubClient(_local_flow_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = auth.run(c, environment="local", username="tester", password="pw-123456")
        self.assertEqual(failed, 0)
        self.assertEqual(c.calls, [
            ("GET", "/decks", None),
            ("login", "tester", None),      # 错误密码 401
            ("register", "tester", None),   # local 先 register
            ("set_token", "tok-u-1", None),
            ("GET", "/auth/me", None),
            ("logout", "", None),
            ("GET", "/auth/me", None),      # logout 后 401
        ])
        self.assertIn("local_test_users_created=1", buf.getvalue())  # 本地新建 user 行计数报告

    def test_run_prod_login_only_never_registers(self) -> None:
        c = stub.StubClient(_prod_flow_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = auth.run(c, environment="prod", username="tester", password="pw-123456")
        self.assertEqual(failed, 0)
        self.assertNotIn(("register", "tester", None), c.calls)  # prod 禁自动注册
        self.assertEqual(c.calls.count(("login", "tester", None)), 2)  # 错误密码 + 正式登录
        self.assertNotIn("报告字段", buf.getvalue())  # prod 不产生本地 user 行

    def test_run_no_session_early_return(self) -> None:
        c = stub.StubClient(stub.script(
            ("/decks", Response(401, {"error": {"code": "AUTH_REQUIRED"}})),
            ("/auth/login", Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})),
            ("/auth/register", Response(0, None)),  # 网络失败不回落 login
        ))
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = auth.run(c, environment="local", username="tester", password="pw-123456")
        self.assertGreater(failed, 0)
        self.assertNotIn(("GET", "/auth/me", None), c.calls)  # 会话失败即止
        self.assertNotIn(("logout", "", None), c.calls)

    def test_run_local_existing_user_login_fallback(self) -> None:
        """账号已存在(409)回落 login,不计数新建 user 行。"""
        login_count = {"n": 0}
        me_count = {"n": 0}

        def handler(method, path, body):
            if path == "/decks":
                return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
            if path == "/auth/register":
                return Response(409, {"error": {"code": "USERNAME_TAKEN"}})
            if path == "/auth/login":
                login_count["n"] += 1
                if login_count["n"] == 1:
                    return Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})  # 错误密码
                return Response(200, stub.session_body("u-1", "tester"))
            if path == "/auth/me":
                me_count["n"] += 1
                if me_count["n"] == 1:
                    return Response(200, ME_BODY)
                return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
            if path == "/auth/logout":
                return Response(204, None)
            return Response(200, {"status": "ok"})

        c = stub.StubClient(handler)
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = auth.run(c, environment="local", username="tester", password="pw-123456")
        self.assertEqual(failed, 0)
        self.assertIn(("register", "tester", None), c.calls)
        self.assertIn(("login", "tester", None), c.calls)
        self.assertNotIn("报告字段", buf.getvalue())  # 回落 login 不新建 user 行


if __name__ == "__main__":
    unittest.main()
