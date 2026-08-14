"""shanka.cost 单元测试:运行前最坏调用预算推导 + 运行后批次对账(废弃「live 固定 3 次调用」假设)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import cost


class FakeScenario:  # 0 LLM 场景:静态声明 LLM_CALLS
    LLM_CALLS = 2


class FakeBudgetScenario:  # live 场景:fixture 推导 LLM_CALLS(与 live_flow 同款模式)
    BUDGET_FIXTURE = {"chapters": 1, "quantity_tendency": "COMPACT", "generate": True}
    LLM_CALLS = 17  # 规划 3 + 生成 9 + 评分 3 + 固定 2


class DeriveBudgetTest(unittest.TestCase):
    def test_live_fixture_worst_case(self) -> None:
        """2 章 × 每章基础 3 单元 × BALANCED 密度 2 = 12 单元;
        规划 1 次任务级调用 × (1+2 重试) = 3;生成 批=单元 12 × (1+2) = 36;
        评分 每单元 1 卡、APPLICATION 逐单元最坏 = 12;固定 api-key 校验 + samples = 2。"""
        b = cost.derive_budget(chapters=2, quantity_tendency="BALANCED", generate=True)
        self.assertEqual(b.units, 12)
        self.assertEqual(b.planning_calls, 3)
        self.assertEqual(b.generation_calls, 36)
        self.assertEqual(b.scoring_calls, 12)
        self.assertEqual(b.fixed_calls, 2)
        self.assertEqual(b.total_calls(), 53)
        self.assertEqual(b.worst_output_tokens(), 3 * 2048 + 36 * 768 + 12 * 4096)
        self.assertEqual(b.worst_input_tokens(), 3 * 20_000 + 36 * 10_000 + 12 * 15_000)
        self.assertAlmostEqual(b.worst_cost_yuan(), 1.863552, places=6)

    def test_density_factors(self) -> None:
        compact = cost.derive_budget(chapters=2, quantity_tendency="COMPACT", generate=True)
        extensive = cost.derive_budget(chapters=2, quantity_tendency="EXTENSIVE", generate=True)
        self.assertEqual(compact.units, 6)
        self.assertEqual(extensive.units, 18)
        self.assertGreater(extensive.total_calls(), compact.total_calls())

    def test_skip_generate_no_pipeline(self) -> None:
        b = cost.derive_budget(chapters=2, quantity_tendency="BALANCED", generate=False)
        self.assertEqual(b.units, 0)
        self.assertEqual(b.planning_calls, 0)
        self.assertEqual(b.generation_calls, 0)
        self.assertEqual(b.scoring_calls, 0)
        self.assertEqual(b.total_calls(), 2)  # api-key 校验 + samples
        self.assertEqual(b.worst_cost_yuan(), 0.0)

    def test_scoring_capped_by_task_limit(self) -> None:
        """300 章 EXTENSIVE:单元数 2700 > 评分调用全局上限 60 → 评分调用封顶 60。"""
        b = cost.derive_budget(chapters=300, quantity_tendency="EXTENSIVE", generate=True)
        self.assertEqual(b.units, 2700)
        self.assertEqual(b.scoring_calls, 60)


class ReconcileTest(unittest.TestCase):
    BUDGET = cost.derive_budget(chapters=2, quantity_tendency="BALANCED", generate=True)

    def test_reconcile_sums_attempts_tokens_cost(self) -> None:
        batches = [
            {"status": "SUCCEEDED", "retry_count": 1,
             "cache_hit_tokens": 100, "cache_miss_tokens": 200, "output_tokens": 50,
             "cost_estimate": 0.001},
            {"status": "SUCCEEDED", "retry_count": 0,
             "cache_hit_tokens": None, "cache_miss_tokens": 300, "output_tokens": None,
             "cost_estimate": 0.002},
        ]
        r = cost.reconcile(batches, self.BUDGET)
        self.assertEqual(r.batch_count, 2)
        self.assertEqual(r.generation_attempts, 3)  # SUCCEEDED:retry_count=失败数,+1
        self.assertEqual(r.tokens, 650)
        self.assertAlmostEqual(r.cost_yuan, 0.003, places=6)
        self.assertTrue(r.within_budget)
        self.assertTrue(r.within_unit_budget)

    def test_reconcile_failed_skipped_retry_count_is_attempts(self) -> None:
        """FAILED/SKIPPED:retry_count 投影 = 账本尝试数(attempt_no),不再 +1。"""
        batches = [
            {"status": "FAILED", "retry_count": 2},
            {"status": "SKIPPED", "retry_count": 3},
        ]
        r = cost.reconcile(batches, self.BUDGET)
        self.assertEqual(r.generation_attempts, 5)

    def test_reconcile_over_budget(self) -> None:
        batches = [{"status": "SUCCEEDED", "retry_count": 2} for _ in range(13)]  # 39 次尝试 > 36
        r = cost.reconcile(batches, self.BUDGET)
        self.assertFalse(r.within_budget)  # 尝试数超预算
        self.assertFalse(r.within_unit_budget)  # 批数 13 > 单元预算 12


class GateTest(unittest.TestCase):
    def test_aggregate_sums(self) -> None:
        self.assertEqual(cost.aggregate([FakeScenario, FakeScenario]), 4)

    def test_budget_for_scenario(self) -> None:
        b = cost.budget_for(FakeBudgetScenario)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.total_calls(), FakeBudgetScenario.LLM_CALLS)
        self.assertIsNone(cost.budget_for(FakeScenario))

    def test_threshold(self) -> None:
        self.assertFalse(cost.requires_confirm(3))  # 阈值不含
        self.assertTrue(cost.requires_confirm(4))  # 超过需确认

    def test_describe_contains_breakdown(self) -> None:
        b = cost.derive_budget(chapters=2, quantity_tendency="BALANCED", generate=True)
        text = cost.describe(b)
        for part in ("PLANNING 3", "GENERATING 36", "SCORING 12", "固定 2", "53", "1.86"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
