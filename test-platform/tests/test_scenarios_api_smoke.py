"""scenarios.baseline.api_smoke 逻辑层单元测试:无网络,StubClient 录制调用序列与报告计数。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.client import Response
from scenarios.baseline import api_smoke
from tests import stub

EMAIL = "tester@local.test"  # 占位凭据(真实凭据只走环境变量)


def _handler():
    decks_get = {"n": 0}

    def handler(method, path, body):
        if path == "/healthz":
            return Response(200, {"status": "ok"})
        if path == "/readyz":
            return Response(200, {"status": "ok"})
        if path == "/decks" and method == "GET":
            decks_get["n"] += 1
            if decks_get["n"] == 1:
                return Response(401, {"error": {"code": "AUTH_REQUIRED"}})  # 无 Bearer
            return Response(200, {"items": []})
        if path == "/decks" and method == "POST" and body == {}:
            return Response(400, {"error": {"code": "VALIDATION_ERROR", "localization_key": "x"}})
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-1", "name": body["name"]})
        if path == "/decks/deck-1" and method == "GET":
            return Response(200, {"deck_id": "deck-1"})
        if method == "DELETE" and path.startswith("/decks/"):
            return Response(204, None)
        if path == "/auth/register":
            return Response(201, stub.session_body("u-1", "tester"))
        if path == "/auth/login":
            return Response(200, stub.session_body("u-1", "tester"))
        if path == "/openapi.json":
            return Response(200, {"paths": {"/decks": {}}})
        if path == "/metrics":
            return Response(200, {"status": "ok"})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


def stub_response(json_body=None):
    """_same_key_post 注入替身:模拟网关 502/HTML 非 JSON 响应体修复后的解析结果。"""
    return (502, json_body)


def _prod_handler():
    decks_get = {"n": 0}

    def handler(method, path, body):
        if path == "/healthz":
            return Response(200, {"status": "ok"})
        if path == "/readyz":
            return Response(200, {"status": "ok"})
        if path == "/decks" and method == "GET":
            decks_get["n"] += 1
            if decks_get["n"] == 1:
                return Response(401, {"error": {"code": "AUTH_REQUIRED"}})
            return Response(200, {"items": []})
        if path == "/decks" and method == "POST" and body == {}:
            return Response(400, {"error": {"code": "VALIDATION_ERROR", "localization_key": "x"}})
        if path == "/decks" and method == "POST":
            return Response(201, {"deck_id": "deck-1", "name": body["name"]})
        if path == "/decks/deck-1" and method == "GET":
            return Response(200, {"deck_id": "deck-1"})
        if method == "DELETE" and path.startswith("/decks/"):
            return Response(204, None)
        if path == "/auth/login":
            return Response(200, stub.session_body("u-1", "tester"))
        if path == "/openapi.json":
            return Response(200, {"paths": {"/decks": {}}})
        if path == "/metrics":
            return Response(200, {"status": "ok"})
        if path == "/auth/logout":
            return Response(204, None)
        return Response(200, {"status": "ok"})

    return handler


class ApiSmokeScenarioTest(unittest.TestCase):
    def test_run_bearer_sequence_and_cleanup(self) -> None:
        replays: list[tuple[str, str, str]] = []

        def same_key_post(c, path, body, idem_key, token):
            replays.append((path, idem_key, token))
            return (201, {"deck_id": "deck-replay"})

        def burst(c, path, n):
            self.assertEqual(n, 6)
            return ([429, 200, 429, 200, 200, 200], ["1", None, "1", None, None, None])

        c = stub.StubClient(_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = api_smoke.run(
                c, environment="local", username="tester", email=EMAIL, password="pw-123456",
                same_key_post=same_key_post, burst=burst,
            )
        self.assertEqual(failed, 0)
        # 会话:local register + set_token + 结束 logout
        self.assertIn(("register", "tester", None), c.calls)
        self.assertIn(("set_token", "tok-u-1", None), c.calls)
        self.assertIn(("logout", "", None), c.calls)
        # 鉴权:无 Bearer 401 在会话建立之前
        self.assertLess(c.calls.index(("GET", "/decks", None)),
                        c.calls.index(("register", "tester", None)))
        # 幂等重放:同键两次且携带 Bearer token
        self.assertEqual(len(replays), 2)
        self.assertEqual(replays[0][1], replays[1][1])
        self.assertEqual(replays[0][2], "tok-u-1")
        # 清理:创建与重放落库的 deck_id 全量删除
        deletes = [p for m, p, _ in c.calls if m == "DELETE" and p.startswith("/decks/")]
        self.assertIn("/decks/deck-1", deletes)
        self.assertIn("/decks/deck-replay", deletes)
        # 本地新建 user 行计数报告
        self.assertIn("local_test_users_created=1", buf.getvalue())

    def test_run_prod_login_only(self) -> None:
        def same_key_post(c, path, body, idem_key, token):
            return (201, {"deck_id": "deck-replay"})

        def burst(c, path, n):
            return ([429, 200, 200, 200, 200, 200], ["1", None, None, None, None, None])

        c = stub.StubClient(_prod_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = api_smoke.run(
                c, environment="prod", username="tester", email=EMAIL, password="pw-123456",
                same_key_post=same_key_post, burst=burst,
            )
        self.assertEqual(failed, 0)
        self.assertNotIn(("register", "tester", None), c.calls)  # prod 禁自动注册
        self.assertIn(("login", EMAIL, None), c.calls)
        self.assertNotIn("报告字段", buf.getvalue())

    def test_run_no_session_early_return(self) -> None:
        c = stub.StubClient(stub.script(
            ("/healthz", Response(200, {"status": "ok"})),
            ("/readyz", Response(200, {"status": "ok"})),
            ("/decks", Response(401, {"error": {"code": "AUTH_REQUIRED"}})),
            ("/auth/register", Response(0, None)),
        ))
        with redirect_stdout(io.StringIO()):
            failed = api_smoke.run(c, environment="local", username="tester", email=EMAIL,
                                   password="pw-123456")
        self.assertGreater(failed, 0)
        self.assertNotIn(("POST", "/decks", None), c.calls)  # 会话失败不进入业务链路
        self.assertNotIn(("logout", "", None), c.calls)

    def test_api_smoke_handles_non_json_response_cleanly(self) -> None:
        """网关 502/HTML 响应时步骤干净 FAIL 而非 AttributeError/JSONDecodeError。"""
        c = stub.StubClient(stub.script(
            ("/healthz", Response(200, {"status": "ok"})),
            ("/readyz", Response(200, {"status": "ok"})),
            ("/decks", Response(502, None)),  # 网关 502/HTML 非 JSON 响应体
            ("/auth/register", Response(201, stub.session_body("u-1", "u"))),
            ("/openapi.json", Response(200, {"paths": {"/decks": {}}})),
            ("/metrics", Response(200, {"status": "ok"})),
            ("/auth/logout", Response(204, None)),
        ))
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = api_smoke.run(
                c, environment="local", username="u", email="u@local.test", password="p",
                same_key_post=lambda *a, **k: stub_response(json_body=None),
                burst=lambda *a, **k: ([], []),  # 空二元组:run 解包不崩,限流断言软 FAIL
            )
        self.assertNotEqual(exit_code, 0)  # 干净 FAIL
        self.assertNotIn("Traceback", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
