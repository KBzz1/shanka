"""成本估算（8.4/O-6）：价格配置常量（生效日期），历史 token 数据不变，调整只改常量。

单价近似（DeepSeek 官方定价量级，标注生效日期；可替换）：
- cache_hit: 0.5 元/百万 token；cache_miss: 2 元/百万；output: 8 元/百万（2026-08-11 起）。
估算只在聚合/观测时计算，token 原始数据永远原样落库（8.4）。
"""

from typing import TypedDict


class _Price(TypedDict):
    effective_date: str
    cache_hit_per_token: float
    cache_miss_per_token: float
    output_per_token: float


_PRICES: list[_Price] = [
    {
        "effective_date": "2026-08-11",
        "cache_hit_per_token": 0.5 / 1_000_000,
        "cache_miss_per_token": 2.0 / 1_000_000,
        "output_per_token": 8.0 / 1_000_000,
    }
]


def _price_for(effective_date: str) -> _Price:
    """生效日期匹配：取 effective_date <= 给定日期 的最新价格档；无匹配取最早档（历史兜底）。"""
    selected = [p for p in _PRICES if p["effective_date"] <= effective_date]
    return selected[-1] if selected else _PRICES[0]


def estimate_cost(
    cache_hit_tokens: int, cache_miss_tokens: int, output_tokens: int, *, effective_date: str
) -> float:
    """总估算金额（元，round 6 位）。价格按生效日期取档；token 数据不在此改写。"""
    price = _price_for(effective_date)
    return round(
        cache_hit_tokens * price["cache_hit_per_token"]
        + cache_miss_tokens * price["cache_miss_per_token"]
        + output_tokens * price["output_per_token"],
        6,
    )


def estimate_cost_by_kind(
    cache_hit_tokens: int, cache_miss_tokens: int, output_tokens: int, *, effective_date: str
) -> dict[str, float]:
    """分项估算 {cache_hit, cache_miss, output, total}（元，8.4 成本汇总出口）。"""
    price = _price_for(effective_date)
    hit = cache_hit_tokens * price["cache_hit_per_token"]
    miss = cache_miss_tokens * price["cache_miss_per_token"]
    output = output_tokens * price["output_per_token"]
    return {
        "cache_hit": round(hit, 6),
        "cache_miss": round(miss, 6),
        "output": round(output, 6),
        "total": round(hit + miss + output, 6),
    }
