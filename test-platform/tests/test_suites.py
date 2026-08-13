"""runner.suites 单元测试:套件构成与闸门逻辑(含 --device-id 移除与凭据缺失拒绝)。"""
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import suites


class SuitesTest(unittest.TestCase):
    def test_suite_membership(self) -> None:
        names = {s.NAME: s for s in suites.SUITES["quick"]}
        self.assertIn("api_smoke", names)
        self.assertNotIn("live_flow", names)
        live_names = {s.NAME for s in suites.SUITES["live"]}
        self.assertIn("live_flow", live_names)

    def test_llm_counts(self) -> None:
        self.assertEqual(suites.llm_total("quick"), 0)
        self.assertGreater(suites.llm_total("live"), suites.llm_total("full"))

    def test_prod_gate(self) -> None:
        self.assertTrue(suites.gate_ok(environment="local", confirm_prod=False))  # local 默认放行
        self.assertTrue(suites.gate_ok(environment="local", confirm_prod=True))
        self.assertFalse(suites.gate_ok(environment="prod", confirm_prod=False))
        self.assertTrue(suites.gate_ok(environment="prod", confirm_prod=True))

    def test_device_id_flag_rejected(self) -> None:
        """--device-id 已删除:argparse 拒绝并退出非 0。"""
        with self.assertRaises(SystemExit) as ctx:
            suites.main(["--device-id", "dev-1"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_credentials_exit_nonzero(self) -> None:
        """凭据 env 缺失 -> 明确报错退出码非 0,且不自动注册(不触发任何 HTTP)。"""
        with mock.patch.dict(os.environ, {}, clear=True):
            code = suites.main(["--environment", "local", "--suite", "quick"])
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
