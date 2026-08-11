"""token 估算模型:Agent 成本/用量观测能力层(事前预估,spec 3.1/3.2)。

估算常量 = 对既有观测数据(Batch 表 cache_hit/miss/output 实际 token,8.3 Cache 指标)
的离线校准值——校准日期与依据见常量注释;换模型/换书籍/实际用量漂移时单点重新校准,
消费方零改动(校准闭环)。价格不在此定义:复用 cost.py 价格档位公开入口(8.4)。
"""

from typing import Any

from services.generation.cost import estimate_cost_by_kind

# 校准常量(2026-08-12,R1 live 实测 deepseek-v4-flash;向上取整偏保守)
PROMPT_TOKENS_PER_KP = 1500  # 实测 1,427/单元(85,599/60)
OUTPUT_TOKENS_PER_KP = 3300  # 实测 3,263/单元(195,774/60)
CUSTOM_REQ_TOKENS_PER_CHAR = 0.5  # 约定:custom_requirements 每字符约 0.5 token

_DENSITY_MULTIPLIER = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}
_BASE_CHUNKS_PER_CHAPTER = 3  # 与 planning._BASE_CHUNKS 同口径(每章基础分块数)


def estimate_tokens(
    chapter_count: int,
    quantity_tendency: str,
    custom_requirements: str | None,
) -> dict[str, int]:
    """输入参数 → 预计 token(与 V4 规划同口径:每章 3×密度系数知识点,每知识点一卡)。"""
    multiplier = _DENSITY_MULTIPLIER.get(quantity_tendency, _DENSITY_MULTIPLIER["BALANCED"])
    kp_count = chapter_count * _BASE_CHUNKS_PER_CHAPTER * multiplier
    prompt_tokens = kp_count * PROMPT_TOKENS_PER_KP
    if custom_requirements:
        prompt_tokens += int(len(custom_requirements) * CUSTOM_REQ_TOKENS_PER_CHAR)
    return {
        "knowledge_point_count": kp_count,
        "estimated_card_count": kp_count,
        "prompt_tokens": prompt_tokens,
        "output_tokens": kp_count * OUTPUT_TOKENS_PER_KP,
    }


def estimate_price_range(
    chapter_count: int,
    quantity_tendency: str,
    custom_requirements: str | None,
    *,
    effective_date: str,
) -> dict[str, Any]:
    """区间估值(8.3 hit/miss 边界):price_low=全命中缓存,price_high=全未命中;output 固定价。

    复用 cost.py 公开入口(生效日期取档),不触碰私有 _price_for、不重复定义价格。
    """
    tokens = estimate_tokens(chapter_count, quantity_tendency, custom_requirements)
    low = estimate_cost_by_kind(
        tokens["prompt_tokens"], 0, tokens["output_tokens"], effective_date=effective_date
    )
    high = estimate_cost_by_kind(
        0, tokens["prompt_tokens"], tokens["output_tokens"], effective_date=effective_date
    )
    return {
        "knowledge_point_count": tokens["knowledge_point_count"],
        "estimated_card_count": tokens["estimated_card_count"],
        "price_low": low["total"],
        "price_high": high["total"],
        "currency": "CNY",
    }
