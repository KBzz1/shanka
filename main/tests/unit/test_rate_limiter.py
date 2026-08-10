"""限流器单元测试（structure-contract 1.6）：固定窗口计数 + Retry-After。"""

from app.middleware.rate_limit import RateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_rate_limiter_allows_within_limit() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    assert limiter.check("dev-1") == (True, 0)
    assert limiter.check("dev-1") == (True, 0)
    assert limiter.check("dev-1") == (True, 0)


def test_rate_limiter_blocks_over_limit_with_retry_after() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("dev-1")
    limiter.check("dev-1")
    allowed, retry_after = limiter.check("dev-1")
    assert allowed is False
    assert retry_after > 0
    assert retry_after <= 60


def test_rate_limiter_window_rolls_over() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("dev-1")
    limiter.check("dev-1")
    clock.advance(61)
    assert limiter.check("dev-1") == (True, 0)


def test_rate_limiter_scopes_isolated() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("a")
    assert limiter.check("b") == (True, 0)
