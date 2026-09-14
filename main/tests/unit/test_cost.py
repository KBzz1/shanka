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


def test_cost_estimate_picks_latest_tier_by_date() -> None:
    """2026-09-12 起切换 deepseek-flash（V4.1-Flash）：hit 单价降档，取新档估算。"""
    old_hit = estimate_cost(
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=0,
        output_tokens=0,
        effective_date="2026-08-11",
    )
    new_hit = estimate_cost(
        cache_hit_tokens=1_000_000,
        cache_miss_tokens=0,
        output_tokens=0,
        effective_date="2026-09-12",
    )
    assert old_hit == 0.5  # 旧档 hit 0.5 元/百万
    assert new_hit == 0.04  # 新档取高峰保守上界 0.04 元/百万
