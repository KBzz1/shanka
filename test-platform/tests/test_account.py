"""shanka.account 单元测试:环境感知注册/登录引导与本地临时测试账号命名。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import account
from shanka.client import Response
from tests import stub


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
        session = account.bootstrap(c, environment="local", username="tester", password="pw-123456")
        self.assertIsNotNone(session)
        self.assertTrue(session["created_local_user"])
        self.assertEqual(session["user_id"], "u-1")
        self.assertEqual(c.calls, [("register", "tester", None), ("set_token", "tok-u-1", None)])

    def test_local_409_user_taken_falls_back_to_login(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/register", Response(409, {"error": {"code": "USERNAME_TAKEN"}})),
            ("/auth/login", Response(200, stub.session_body("u-1", "tester"))),
        ))
        session = account.bootstrap(c, environment="local", username="tester", password="pw-123456")
        self.assertIsNotNone(session)
        self.assertFalse(session["created_local_user"])
        self.assertEqual(
            c.calls,
            [("register", "tester", None), ("login", "tester", None), ("set_token", "tok-u-1", None)],
        )

    def test_local_register_other_failure_no_fallback(self) -> None:
        """网络失败(0)/429 等不静默回落 login,由调用方报告。"""
        c = stub.StubClient(stub.script(("/auth/register", Response(0, None))))
        self.assertIsNone(
            account.bootstrap(c, environment="local", username="tester", password="pw-123456")
        )
        self.assertEqual(c.calls, [("register", "tester", None)])

    def test_prod_login_only_never_registers(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(200, stub.session_body("u-1", "tester"))),
        ))
        session = account.bootstrap(c, environment="prod", username="tester", password="pw-123456")
        self.assertIsNotNone(session)
        self.assertFalse(session["created_local_user"])
        self.assertEqual(c.calls, [("login", "tester", None), ("set_token", "tok-u-1", None)])

    def test_login_failure_returns_none(self) -> None:
        c = stub.StubClient(stub.script(
            ("/auth/login", Response(401, {"error": {"code": "INVALID_CREDENTIALS"}})),
        ))
        self.assertIsNone(
            account.bootstrap(c, environment="prod", username="tester", password="pw-123456")
        )
        self.assertEqual(c.calls, [("login", "tester", None)])


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
