"""runner.suites 单元测试:套件构成与闸门逻辑(含 --device-id 移除、凭据缺失拒绝、注入参数)。"""
import io
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import suites


class SuitesTest(unittest.TestCase):
    def test_suite_membership(self) -> None:
        quick_names = {s.NAME for s in suites.SUITES["quick"]}
        self.assertIn("api_smoke", quick_names)
        self.assertIn("auth", quick_names)
        self.assertNotIn("live_flow", quick_names)
        full_names = {s.NAME for s in suites.SUITES["full"]}
        self.assertIn("isolation", full_names)
        live_names = {s.NAME for s in suites.SUITES["live"]}
        self.assertIn("live_flow", live_names)
        self.assertIn("isolation", live_names)

    def test_llm_counts(self) -> None:
        self.assertEqual(suites.llm_total("quick"), 0)
        self.assertEqual(suites.llm_total("full"), 0)
        self.assertGreater(suites.llm_total("live"), suites.llm_total("full"))
        # live_flow 调用数为 fixture 推导的最坏预算(废弃固定 3 假设),超默认阈值
        from scenarios.flow import live_flow
        self.assertEqual(suites.llm_total("live"), live_flow.LLM_CALLS)
        self.assertGreater(suites.llm_total("live"), 3)
        self.assertTrue(suites.cost.requires_confirm(suites.llm_total("live")))

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

    def test_runner_injects_environment_and_run_id(self) -> None:
        """runner 向每个场景注入 --base-url/--environment/--run-id(场景不自行生成 run_id)。"""
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(suites.auth, "main", return_value=0) as m_auth, \
                 mock.patch.object(suites.api_smoke, "main", return_value=0) as m_smoke:
                code = suites.main(["--environment", "local", "--suite", "quick"])
        self.assertEqual(code, 0)
        for m in (m_auth, m_smoke):
            args = m.call_args.args[0]
            self.assertIn("--base-url", args)
            self.assertIn("--environment", args)
            self.assertEqual(args[args.index("--environment") + 1], "local")
            run_id = args[args.index("--run-id") + 1]
            self.assertTrue(run_id)  # 非空 UUID 注入

    def test_live_suite_requires_confirm_cost(self) -> None:
        """live 最坏预算超阈值:无 --confirm-cost 拒绝执行(exit 1),有则放行(进入场景调度)。"""
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            code = suites.main(["--environment", "local", "--suite", "live"])
        self.assertEqual(code, 1)
        env = {"SHANKA_TEST_USERNAME": "u", "SHANKA_TEST_PASSWORD": "p"}
        with mock.patch.dict(os.environ, env):
            with mock.patch.object(suites.auth, "main", return_value=0) as m_auth, \
                 mock.patch.object(suites.isolation, "main", return_value=0) as m_iso, \
                 mock.patch.object(suites.api_smoke, "main", return_value=0) as m_smoke, \
                 mock.patch.object(suites.live_flow, "main", return_value=0) as m_live, \
                 mock.patch("sys.stdout", io.StringIO()) as out:
                code = suites.main(
                    ["--environment", "local", "--suite", "live", "--confirm-cost"])
        self.assertEqual(code, 0)
        self.assertTrue(m_auth.called and m_iso.called and m_smoke.called and m_live.called)
        self.assertIn("成本闸门: --confirm-cost 已确认", out.getvalue())


if __name__ == "__main__":
    unittest.main()
