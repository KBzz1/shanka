"""infra.clock 时钟唯一入口单元测试（Progress 2.5：时钟唯一入口）。"""

from datetime import UTC, datetime

from infra.clock import FrozenClock, SystemClock


def test_clock_system_now_utc_aware() -> None:
    now = SystemClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


def test_clock_frozen_returns_fixed_value() -> None:
    fixed = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
    assert FrozenClock(fixed).now_utc() == fixed
