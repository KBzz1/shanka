"""shanka.environments 单元测试:环境目标与测试账号凭据 env 读取。"""
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.environments import (
    ENVIRONMENTS,
    MissingCredentialsError,
    credentials,
    is_prod,
    resolve,
)


class EnvironmentsTest(unittest.TestCase):
    def test_local_and_prod(self) -> None:
        self.assertEqual(resolve("local"), "http://localhost:8000")
        self.assertEqual(resolve("prod"), "https://shanka.kbzz1.top")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve("nonexistent")

    def test_is_prod(self) -> None:
        self.assertFalse(is_prod("local"))
        self.assertTrue(is_prod("prod"))


class CredentialsTest(unittest.TestCase):
    def test_credentials_from_env(self) -> None:
        with mock.patch.dict(
            os.environ, {"SHANKA_TEST_USERNAME": "u1", "SHANKA_TEST_EMAIL": "u1@local.test",
                         "SHANKA_TEST_PASSWORD": "p1"}
        ):
            self.assertEqual(credentials(), ("u1", "u1@local.test", "p1"))

    def test_credentials_missing_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingCredentialsError) as ctx:
                credentials()
        msg = str(ctx.exception)
        self.assertIn("SHANKA_TEST_USERNAME", msg)
        self.assertIn("SHANKA_TEST_EMAIL", msg)
        self.assertIn("SHANKA_TEST_PASSWORD", msg)
        self.assertIn("不自动注册", msg)

    def test_credentials_partial_missing_names_only_missing(self) -> None:
        with mock.patch.dict(os.environ, {"SHANKA_TEST_USERNAME": "u1"}, clear=True):
            with self.assertRaises(MissingCredentialsError) as ctx:
                credentials()
        msg = str(ctx.exception)
        self.assertIn("SHANKA_TEST_PASSWORD", msg)
        self.assertIn("SHANKA_TEST_EMAIL", msg)
        self.assertNotIn("SHANKA_TEST_USERNAME", msg)


if __name__ == "__main__":
    unittest.main()
