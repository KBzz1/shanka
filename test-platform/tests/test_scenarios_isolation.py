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

EMAIL = "tester@local.test"  # 占位凭据(真实凭据只走环境变量)


def _local_handler(evidence: dict | None = None):
    """auth 感知的本地后端替身:按 token 分派,模拟按用户隔离与幂等重放。

    5 参 handler(method, path, body, auth, idempotency_key):auth 为当前 token
    (register/login 路径可为 None);evidence 非 None 时记录显式幂等键的
    keys_by_user/writes/replays/deletes 供跨用户幂等测试断言。
    """
    state = {"seq": 0, "decks": {}, "idem": {}}
    ev = evidence if evidence is not None else {}

    def handler(method, path, body, auth, idempotency_key):
        if path == "/auth/register":
            username = body["username"]
            return Response(201, stub.session_body(f"u-{username}", username))
        if path == "/auth/login":
            email = body["email"]
            return Response(200, stub.session_body(f"u-{email}", email))
        if path == "/decks" and method == "POST":
            if idempotency_key:
                slot = (auth, idempotency_key)
                ev.setdefault("keys_by_user", {}).setdefault(auth, []).append(idempotency_key)
                if slot in state["idem"]:
                    replays = ev.setdefault("replays", {})
                    replays[slot] = replays.get(slot, 0) + 1
                    return state["idem"][slot]  # 同 key 重放:原响应不新建
            state["seq"] += 1
            deck_id = f"deck-{state['seq']}"
            resp = Response(201, {"deck_id": deck_id, "name": body["name"]})
            state["decks"][deck_id] = (auth, body["name"])
            if idempotency_key:
                state["idem"][slot] = resp
                ev.setdefault("writes", {})[slot] = deck_id
            return resp
        if path == "/decks" and method == "GET":
            items = [{"deck_id": d, "name": n} for d, (o, n) in state["decks"].items() if o == auth]
            return Response(200, {"items": items})
        if path.startswith("/decks/") and method == "GET":
            deck_id = path[len("/decks/"):]
            owner = state["decks"].get(deck_id, (None, ""))[0]
            if owner is None or owner != auth:
                return Response(404, {"error": {"code": "NOT_FOUND"}})
            return Response(200, {"deck_id": deck_id})
        if path.startswith("/decks/") and method == "DELETE":
            deck_id = path[len("/decks/"):]
            owner = state["decks"].get(deck_id, (None, ""))[0]
            if owner is None or owner != auth:
                return Response(404, {"error": {"code": "NOT_FOUND"}})  # 跨用户删除不生效
            del state["decks"][deck_id]
            ev.setdefault("deletes", {})[deck_id] = auth
            return Response(204, None)
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
                c, environment="local", username="tester", email=EMAIL, password="pw-123456",
                run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
            )
        self.assertEqual(failed, 0)
        calls = c.calls
        # 主账号 + run_id 命名临时账号
        self.assertEqual([u for op, u, _ in calls if op == "register"],
                         ["tester", "t-3f2a9c81d4-iso"])
        # 跨用户读/写 404 与列表隔离断言对应的调用(主牌组 deck-1)
        self.assertIn(("GET", "/decks/deck-1", None), calls)
        self.assertIn(("DELETE", "/decks/deck-1", None), calls)
        # 主账号切回(临时账号注销后 set_token 主 token)+ 两 session 注销
        self.assertIn(("set_token", "tok-u-tester", None), calls)
        self.assertEqual(calls.count(("logout", "", None)), 2)
        # 跨用户幂等步骤:两账号 idem 牌组各自前缀清理
        self.assertIn("跨用户幂等牌组已清理", buf.getvalue())
        # 无法安全删除的 user 行(主账号新建 1 + 临时账号 1)按 run 计数报告
        self.assertIn("local_test_users_created=2", buf.getvalue())

    def test_isolation_idempotency_key_cross_user_reuse(self) -> None:
        """不同用户同 Idempotency-Key 同 body:各自成功、互不重放(DESIGN 8.2 缺口)。"""
        evidence: dict = {}
        c = stub.StubClient(_local_handler(evidence))
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="local", username="tester", email=EMAIL, password="pw-123456",
                run_id="3f2a9c81d4e54b679c1d2e3f4a5b6c7d",
            )
        self.assertEqual(failed, 0)
        main = "tok-u-tester"
        second = "tok-u-t-3f2a9c81d4-iso"
        # 同一幂等键被两用户复用:主账号 2 次(创建 + 重放)、临时账号 1 次
        keys_main = evidence.get("keys_by_user", {}).get(main, [])
        keys_second = evidence.get("keys_by_user", {}).get(second, [])
        self.assertEqual(len(keys_main), 2)
        self.assertEqual(keys_main, keys_second * 2)
        key = keys_second[0]
        # 同 key 同 body:两个用户各建一张牌组,deck_id 不同
        main_deck_id = evidence.get("writes", {}).get((main, key))
        second_deck_id = evidence.get("writes", {}).get((second, key))
        self.assertEqual(len(evidence.get("writes", {})), 2)  # 显式 key 仅两用户各一张
        self.assertIsNotNone(main_deck_id)
        self.assertIsNotNone(second_deck_id)
        self.assertNotEqual(main_deck_id, second_deck_id)
        # 主账号重放同 key 同 body:返回原响应、不新建
        self.assertEqual(evidence.get("replays", {}).get((main, key)), 1)
        # 清理:两账号 idem 牌组各自删除(前缀清理命中各自归属)
        self.assertEqual(evidence.get("deletes", {}).get(main_deck_id), main)
        self.assertEqual(evidence.get("deletes", {}).get(second_deck_id), second)

    def test_run_prod_read_only(self) -> None:
        c = stub.StubClient(_prod_handler())
        buf = io.StringIO()
        with redirect_stdout(buf):
            failed = isolation.run(
                c, environment="prod", username="tester", email=EMAIL, password="pw-123456",
                run_id="run-1",
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
                c, environment="local", username="tester", email=EMAIL, password="pw-123456",
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
                c, environment="local", username="tester", email=EMAIL, password="pw-123456",
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
                c, environment="prod", username="tester", email=EMAIL, password="pw-123456",
                run_id="run-1",
            )
        self.assertGreater(failed, 0)
        self.assertEqual(c.calls, [("login", EMAIL, None)])


if __name__ == "__main__":
    unittest.main()
