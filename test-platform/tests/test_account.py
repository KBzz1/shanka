"""shanka.account 单元测试:环境感知注册/登录引导、429 显式重试与本地临时测试账号命名。

429 重试用假 Response + 假 client(StubClient),不触网;time.sleep 用 unittest.mock
替掉并断言被调用。
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import account
from shanka.client import Response
from tests import stub

EMAIL = "tester@local.test"


class AuthModeTest(unittest.TestCase):
    def test_local_register_prod_login(self) -> None:
        self.assertEqual(account.auth_mode("local"), "register")
        self.assertEqual(account.auth_mode("prod"), "login")


class ParseSessionTest(unittest.TestCase):
    def test_valid_session(self) -> None:
        r = Response(201, stub.session_body("u-1", "tester"))
        self.assertEqual(
            account.parse_session(r),
            {"user_id": "u-1", "username": "tester", "access_token": "tok-u-1"},
        )

    def test_invalid_shapes_return_none(self) -> None:
        cases = [
            Response(200, None),                                   # 非 JSON 响应体
            Response(200, {}),                                     # 空体
            Response(200, {"access_token": "t"}),                  # 缺 user
            Response(200, {"user": {}, "access_token": "t"}),      # user 缺字段
            Response(200, {"user": {"user_id": "u", "username": "n"}}),  # 缺 token
            Response(200, {"user": {"user_id": "u", "username": "n"}, "access_token": 5}),
        ]
        for r in cases:
            self.assertIsNone(account.parse_session(r))


class BootstrapTest(unittest.TestCase):
    def test_local_register_success_sets_token(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/register", Response(201, stub.session_body("u-1", "tester"))),
        ))
        session = account.bootstrap(c, environment="local", username="tester",
                                    email=EMAIL, password="pw-123456")
        self.assertIsNotNone(session)
        self.assertTrue(session["created_local_user"])
        self.assertEqual(session["user_id"], "u-1")
        self.assertEqual(c.calls, [("register", "tester", None), ("set_token", "tok-u-1", None)])

    def test_local_409_email_taken_falls_back_to_login_with_email(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/register", Response(409, {"error": {"code": "EMAIL_TAKEN"}})),
            ("/auth/login", Response(200, stub.session_body("u-1", "tester"))),
        ))
        session = account.bootstrap(c, environment="local", username="tester",
                                    email=EMAIL, password="pw-123456")
        self.assertIsNotNone(session)
        self.assertFalse(session["created_local_user"])
        self.assertEqual(
            c.calls,
            [("register", "tester", None),
             ("login", EMAIL, None),   # 回落 login 以 email 为登录键
             ("set_token", "tok-u-1", None)],
        )

    def test_local_register_other_failure_no_fallback(self) -> None:
        """网络失败(0)/5xx 等不静默回落 login,由调用方报告。"""
        c = stub.StubClient(stub.script(("/auth/register", Response(0, None))))
        self.assertIsNone(
            account.bootstrap(c, environment="local", username="tester",
                              email=EMAIL, password="pw-123456")
        )
        self.assertEqual(c.calls, [("register", "tester", None)])

    def test_prod_login_only_never_registers(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(200, stub.session_body("u-1", "tester"))),
        ))
        session = account.bootstrap(c, environment="prod", username="tester",
                                    email=EMAIL, password="pw-123456")
        self.assertIsNotNone(session)
        self.assertFalse(session["created_local_user"])
        self.assertEqual(c.calls, [("login", EMAIL, None), ("set_token", "tok-u-1", None)])

    def test_login_failure_returns_none(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})),
        ))
        self.assertIsNone(
            account.bootstrap(c, environment="prod", username="tester",
                              email=EMAIL, password="pw-123456")
        )
        self.assertEqual(c.calls, [("login", EMAIL, None)])


def _retry_register_201_handler():
    """第一次 register 返回 429(retry-after=1),第二次返回 201 会话形状。"""
    n = {"register": 0, "login": 0}

    def handler(method, path, body):
        if path == "/auth/register":
            n["register"] += 1
            if n["register"] == 1:
                return Response(429, {"error": {"code": "RATE_LIMITED"}}, headers={"retry-after": "1"})
            return Response(201, stub.session_body("u-1", "tester"))
        if path == "/auth/login":
            n["login"] += 1
            return Response(200, stub.session_body("u-1", "tester"))
        return Response(200, {"status": "ok"})

    return handler


def _always_429_handler():
    def handler(method, path, body):
        return Response(429, {"error": {"code": "RATE_LIMITED"}}, headers={"retry-after": "1"})

    return handler


class Bootstrap429RetryTest(unittest.TestCase):
    """bootstrap 对 register/login 的 429 显式重试(限 3 次,按 Retry-After 等待)。

    429 是服务端业务执行前的明确拒绝(限流桶检查先于建用户/建会话),重试无重放副作用;
    与 FR-19 防超时/5xx 未知结果重放不冲突。
    """

    def test_register_429_then_201_retries_and_succeeds(self) -> None:
        """第一次 register 429(retry-after=1) → 按 Retry-After 等待重试 → 201 成功。"""
        c = stub.StubClient(_retry_register_201_handler())
        with mock.patch.object(account.time, "sleep", return_value=None) as sleep_mock, \
                redirect_stdout(io.StringIO()):
            session = account.bootstrap(c, environment="local", username="tester",
                                        email=EMAIL, password="pw-123456")
        self.assertIsNotNone(session)
        self.assertTrue(session["created_local_user"])
        self.assertEqual(session["user_id"], "u-1")
        # retry-after=1 → 等待 2s(1+1),且 register 被调用两次
        self.assertEqual(sleep_mock.call_args.args, (2,))
        self.assertEqual(c.calls.count(("register", "tester", None)), 2)
        self.assertIn(("set_token", "tok-u-1", None), c.calls)

    def test_login_429_three_times_then_fails_with_none(self) -> None:
        """login 连续 429 三次重试后仍 429 → bootstrap 返回 None(打印指引不抛异常)。"""
        c = stub.StubClient(_always_429_handler())
        with mock.patch.object(account.time, "sleep", return_value=None), \
                redirect_stdout(io.StringIO()) as out:
            session = account.bootstrap(c, environment="prod", username="tester",
                                        email=EMAIL, password="pw-123456")
        self.assertIsNone(session)
        # 首次调用 + 3 次重试 = 4 次 login 调用(每次 429 后等待 2s)
        self.assertEqual(c.calls.count(("login", EMAIL, None)), 4)
        self.assertIn("[FAIL] login 限流重试 3 次后仍 429", out.getvalue())

    def test_register_409_falls_back_to_login_with_email(self) -> None:
        """register 409 EMAIL_TAKEN → 回落 login(email),断言 login 收到的 email。"""
        login_n = {"n": 0}

        def handler(method, path, body):
            if path == "/auth/register":
                return Response(409, {"error": {"code": "EMAIL_TAKEN"}})
            if path == "/auth/login":
                login_n["n"] += 1
                return Response(200, stub.session_body("u-1", "tester"))
            return Response(200, {"status": "ok"})

        c = stub.StubClient(handler)
        with redirect_stdout(io.StringIO()):
            session = account.bootstrap(c, environment="local", username="tester",
                                        email=EMAIL, password="pw-123456")
        self.assertIsNotNone(session)
        self.assertFalse(session["created_local_user"])
        self.assertEqual(login_n["n"], 1)
        self.assertEqual(c.calls.count(("login", EMAIL, None)), 1)  # login 以 email 为键
        self.assertEqual(c.calls.count(("login", "tester", None)), 0)  # 绝不以 username 登录


class TempAccountTest(unittest.TestCase):
    def test_username_charset_and_length(self) -> None:
        name = account.temp_username("3f2a9c81-d4e5-4b67-9c1d-2e3f4a5b6c7d", "iso")
        self.assertRegex(name, r"^[a-z0-9-]{3,32}$")  # 用户名规则(4.2):小写字母/数字/- 3~32 位
        self.assertNotIn(".", name)
        self.assertNotIn("_", name)

    def test_username_distinct_by_tag(self) -> None:
        self.assertNotEqual(
            account.temp_username("abc123", "iso"), account.temp_username("abc123", "obs")
        )

    def test_temp_email_matches_username(self) -> None:
        run_id = "3f2a9c81d4e54b679c1d2e3f4a5b6c7d"
        self.assertEqual(account.temp_email(run_id, "iso"),
                         f"{account.temp_username(run_id, 'iso')}@local.test")
        self.assertNotEqual(account.temp_email(run_id, "iso"),
                            account.temp_email(run_id, "obs"))

    def test_password_random_and_long_enough(self) -> None:
        p1, p2 = account.temp_password(), account.temp_password()
        self.assertGreaterEqual(len(p1), 8)
        self.assertLessEqual(len(p1), 128)
        self.assertNotEqual(p1, p2)


class WrongPasswordTest(unittest.TestCase):
    def test_same_length_and_different(self) -> None:
        pw = "pw-123456"
        wrong = account.wrong_password(pw)
        self.assertEqual(len(wrong), len(pw))  # 等长避免触发 400 长度校验差异
        self.assertNotEqual(wrong, pw)

    def test_tail_swap(self) -> None:
        self.assertEqual(account.wrong_password("abcx"), "abcy")
        self.assertEqual(account.wrong_password("abcy"), "abcx")


if __name__ == "__main__":
    unittest.main()
