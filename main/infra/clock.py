"""时钟唯一入口（Progress 2.5）：服务端为权威时钟（structure-contract 1.2）。"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """测试用可控时钟（F0 测试基座）。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now
