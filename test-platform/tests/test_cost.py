"""shanka.cost 单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost


class FakeScenario:  # 模拟场景模块:声明 LLM_CALLS
    LLM_CALLS = 2


class CostTest(unittest.TestCase):
    def test_aggregate_sums(self) -> None:
        self.assertEqual(cost.aggregate([FakeScenario, FakeScenario]), 4)

    def test_threshold(self) -> None:
        self.assertFalse(cost.requires_confirm(3))   # 阈值不含
        self.assertTrue(cost.requires_confirm(4))    # 超过需确认


if __name__ == "__main__":
    unittest.main()
