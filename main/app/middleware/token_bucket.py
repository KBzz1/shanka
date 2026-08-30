"""IP token bucket（structure-contract 1.6「IP 5 req/s：全部接口」的持续速率语义）。

固定窗口（RateLimiter）把正常移动端启动/刷新短突发变成 429；本模块实现
「持续速率 + 短突发」：

- 每 key（IP）独立桶，首次到达按满桶初始（capacity 个 token）——突发可立即消费；
- 持续 refill：`rate_per_second` 个 token/秒（含分数折算，向上限容量封顶；
  rate=5 → 每 0.2 秒补充 1 个 token）；
- `check(key) -> (allowed, retry_after)`：有 token → 消耗 1 放行；无 token →
  拒绝，retry_after = 下一 token 可用时间向上取整、最少 1 秒（整数响应头）。
- 状态有界：key 数达 max_keys 后仅淘汰「语义上已回满」的 idle key（refill 后
  全量，淘汰无配额损失）；未满桶不可淘汰——淘汰会让活跃 IP 绕过持续速率。
  无满桶可淘汰时新 key 拒绝且不入表（拒绝路径不放大状态）。
- 确定性：测试注入固定 clock（callable 或 `.now() -> float` 对象，同 RateLimiter）。

业务长窗口（write/api_key/samples/pdf/auth）仍由 rate_limit.py 的固定窗口
RateLimiter 承担，本模块不改变其语义。
"""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class ClockLike(Protocol):
    """可注入时钟（与 rate_limit.RateLimiter 的 ClockLike 同形：`.now() -> float`）。"""

    def now(self) -> float: ...


# 消费/满桶判定的浮点容差：消除 refill 折算的边界误差（0.19*5 类序列），
# 时间含义 < 1e-9/rate 秒，远小于任何有意义的时间粒度。
_EPS = 1e-9


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucket:
    """IP 维度 token bucket：持续 refill rate + 突发容量，状态按 key 隔离且有界。"""

    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        *,
        clock: Callable[[], float] | ClockLike | None = None,
        max_keys: int = 10_000,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second 必须为正")
        if capacity <= 0:
            raise ValueError("capacity 必须为正")
        if max_keys <= 0:
            raise ValueError("max_keys 必须为正")
        self._rate = float(rate_per_second)
        self._capacity = capacity
        self._max_keys = max_keys
        self._clock: Callable[[], float] | ClockLike = (
            clock if clock is not None else time.monotonic
        )
        self._buckets: dict[str, _Bucket] = {}

    def __repr__(self) -> str:
        return (
            f"TokenBucket(rate_per_second={self._rate}, capacity={self._capacity}, "
            f"max_keys={self._max_keys}, tracked={len(self._buckets)})"
        )

    @property
    def tracked_key_count(self) -> int:
        return len(self._buckets)

    def _now(self) -> float:
        clock = self._clock
        if callable(clock):
            return clock()
        return clock.now()

    def check(self, key: str) -> tuple[bool, int]:
        """检查并消费一个 token：`(是否放行, Retry-After 秒)`。

        拒绝时 Retry-After 为下一个 token 可用时间的向上取整，最少 1 秒、整数。
        """
        now = self._now()
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_keys:
                # 表满：先淘汰语义上已回满的 idle key；无候选 → 拒绝且不入表
                self._evict_full_idle(now)
                if len(self._buckets) >= self._max_keys:
                    return False, self._retry_after(0.0)
            # 首次到达按满桶初始（短突发语义；时间推进后可立即消费满容量）
            bucket = _Bucket(tokens=float(self._capacity), updated_at=now)
            self._buckets[key] = bucket
        return self._consume(bucket, now)

    def _consume(self, bucket: _Bucket, now: float) -> tuple[bool, int]:
        tokens = self._refill(bucket, now)
        if tokens >= 1.0 - _EPS:
            bucket.tokens = max(0.0, tokens - 1.0)
            return True, 0
        # 拒绝值写回（refill 已消费），下次 check 从当前判定时刻起算
        bucket.tokens = tokens
        return False, self._retry_after(tokens)

    def _refill(self, bucket: _Bucket, now: float) -> float:
        elapsed = now - bucket.updated_at
        if elapsed > 0.0:
            bucket.tokens = min(float(self._capacity), bucket.tokens + elapsed * self._rate)
            bucket.updated_at = now
        return bucket.tokens

    def _retry_after(self, tokens: float) -> int:
        return max(1, math.ceil(max(0.0, 1.0 - tokens) / self._rate))

    def _evict_full_idle(self, now: float) -> None:
        """淘汰一个「refill 后满桶」的 idle key（无配额损失，重建即等价；仅淘汰满桶）。

        一个 key 一旦回满，其状态不再携带任何扣减信息——淘汰它不会让它的持续
        速率被绕过（re-enter 时按满桶重建）。未满桶持有真实欠额，必须保留。
        """
        for key, bucket in list(self._buckets.items()):
            if self._refill(bucket, now) >= float(self._capacity) - _EPS:
                del self._buckets[key]
                return  # 每轮最多淘汰一个满桶候选（循环检查驱逐，摊薄扫描成本）
