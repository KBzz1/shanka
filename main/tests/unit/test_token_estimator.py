"""token 估算模型单元测试(Agent 成本观测能力层,spec 3.1/3.2)。"""

from services.generation.token_estimator import (
    OUTPUT_TOKENS_PER_KP,
    PROMPT_TOKENS_PER_KP,
    estimate_price_range,
    estimate_tokens,
)


def test_density_matches_v4_planning() -> None:
    # 每章 3×密度系数(COMPACT=1/BALANCED=2/EXTENSIVE=3),2 章 = 6/12/18(V4 实测口径)
    assert estimate_tokens(2, "COMPACT", None)["knowledge_point_count"] == 6
    assert estimate_tokens(2, "BALANCED", None)["knowledge_point_count"] == 12
    assert estimate_tokens(2, "EXTENSIVE", None)["knowledge_point_count"] == 18


def test_unknown_tendency_falls_back_balanced() -> None:
    # 与 planning._DENSITY.get(tendency, 2) 同口径:未知值防御性回落 BALANCED
    assert (
        estimate_tokens(1, "BALANCED", None)["knowledge_point_count"]
        == estimate_tokens(1, "UNKNOWN", None)["knowledge_point_count"]
    )


def test_token_constants_and_card_count() -> None:
    t = estimate_tokens(1, "COMPACT", None)  # 1 章 COMPACT = 3 知识点
    assert t["estimated_card_count"] == 3
    assert t["prompt_tokens"] == 3 * PROMPT_TOKENS_PER_KP
    assert t["output_tokens"] == 3 * OUTPUT_TOKENS_PER_KP


def test_custom_requirements_add_prompt_tokens() -> None:
    base = estimate_tokens(1, "COMPACT", None)["prompt_tokens"]
    with_custom = estimate_tokens(1, "COMPACT", "a" * 10)[
        "prompt_tokens"
    ]  # 10 字符 × 0.5 = 5 token
    assert with_custom == base + 5


def test_price_range_exact_values() -> None:
    # 1 章 COMPACT = 3 KP:prompt 4500 / output 9900(deepseek-v4-flash,2026-08-12 价格档)
    # low = 4500×0.5/M + 9900×8/M = 0.00225 + 0.0792 = 0.08145
    # high = 4500×2/M + 9900×8/M = 0.009 + 0.0792 = 0.0882
    r = estimate_price_range(1, "COMPACT", None, effective_date="2026-08-12")
    assert r["knowledge_point_count"] == 3
    assert r["estimated_card_count"] == 3
    assert r["price_low"] == 0.08145
    assert r["price_high"] == 0.0882
    assert r["currency"] == "CNY"
    assert 0 < r["price_low"] <= r["price_high"]
