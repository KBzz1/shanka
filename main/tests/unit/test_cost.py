"""services.generation.cost 成本估算单元测试（8.4：价格常量按生效日期，历史 token 不变）。"""

from services.generation.cost import estimate_cost


def test_cost_estimate_positive() -> None:
    cost = estimate_cost(
        cache_hit_tokens=1000,
        cache_miss_tokens=1000,
        output_tokens=500,
        effective_date="2026-08-11",
    )
    assert cost > 0


def test_cost_estimate_uses_hit_and_miss_rates() -> None:
    a = estimate_cost(
        cache_hit_tokens=2000, cache_miss_tokens=0, output_tokens=0, effective_date="2026-08-11"
    )
    b = estimate_cost(
        cache_hit_tokens=0, cache_miss_tokens=2000, output_tokens=0, effective_date="2026-08-11"
    )
    assert b > a  # miss 单价 > hit 单价


def test_cost_estimate_zero_inputs() -> None:
    assert estimate_cost(0, 0, 0, effective_date="2026-08-11") == 0.0
