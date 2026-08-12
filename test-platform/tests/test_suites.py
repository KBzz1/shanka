"""runner.suites 单元测试:套件构成与闸门逻辑。"""
import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
