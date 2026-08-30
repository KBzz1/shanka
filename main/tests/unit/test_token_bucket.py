"""IP token bucket unit tests: burst, refill, isolation, Retry-After, and bounded state."""

import pytest

from app.middleware.token_bucket import TokenBucket


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_token_bucket_allows_initial_burst_then_limits_sustained_rate() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(rate_per_second=5, capacity=10, clock=clock)

    assert [bucket.check("198.51.100.1") for _ in range(10)] == [(True, 0)] * 10
    assert bucket.check("198.51.100.1") == (False, 1)


def test_token_bucket_refills_one_token_every_point_two_seconds_without_overfill() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(rate_per_second=5, capacity=10, clock=clock)
    for _ in range(10):
        assert bucket.check("198.51.100.1") == (True, 0)

    clock.advance(0.19)
    assert bucket.check("198.51.100.1") == (False, 1)
    clock.advance(0.01)
    assert bucket.check("198.51.100.1") == (True, 0)
    assert bucket.check("198.51.100.1") == (False, 1)

    clock.advance(100.0)
    assert [bucket.check("198.51.100.1") for _ in range(10)] == [(True, 0)] * 10
    assert bucket.check("198.51.100.1") == (False, 1)


def test_token_bucket_isolates_keys() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(rate_per_second=1, capacity=1, clock=clock)

    assert bucket.check("198.51.100.1") == (True, 0)
    assert bucket.check("198.51.100.1") == (False, 1)
    assert bucket.check("203.0.113.2") == (True, 0)


def test_token_bucket_retry_after_is_positive_integer_ceiling() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(rate_per_second=0.25, capacity=1, clock=clock)
    assert bucket.check("198.51.100.1") == (True, 0)

    clock.advance(1.1)
    assert bucket.check("198.51.100.1") == (False, 3)


def test_token_bucket_evicts_only_semantically_full_idle_keys() -> None:
    clock = _FakeClock()
    bucket = TokenBucket(rate_per_second=1, capacity=1, clock=clock, max_keys=2)

    assert bucket.check("198.51.100.1") == (True, 0)
    assert bucket.check("198.51.100.2") == (True, 0)
    assert bucket.tracked_key_count == 2
    assert bucket.check("198.51.100.3") == (False, 1)
    assert bucket.tracked_key_count == 2

    clock.advance(1.0)
    assert bucket.check("198.51.100.3") == (True, 0)
    assert bucket.tracked_key_count <= 2


@pytest.mark.parametrize(
    ("rate", "capacity", "max_keys"),
    [(0, 1, 1), (-1, 1, 1), (1, 0, 1), (1, -1, 1), (1, 1, 0)],
)
def test_token_bucket_rejects_non_positive_configuration(
    rate: float, capacity: int, max_keys: int
) -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_second=rate, capacity=capacity, max_keys=max_keys)
