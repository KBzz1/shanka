"""成本护栏:真实 DeepSeek 调用(LLM_CALLS)聚合与阈值闸门。

计数口径:PUT /api-key 校验、POST /samples、POST /tasks 均计 1 次。
"""

from typing import Any

THRESHOLD = 3  # 超过此数(> 3)必须 --confirm-cost


def aggregate(scenarios: list[Any]) -> int:
    return sum(int(getattr(s, "LLM_CALLS", 0)) for s in scenarios)


def requires_confirm(total: int) -> bool:
    return total > THRESHOLD
