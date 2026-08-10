"""infra.db.session.format_utc 统一时间格式单元测试（database-design 0）。"""

from datetime import UTC, datetime

import pytest

from infra.db.session import format_utc


def test_format_utc_fixed_3ms_and_z_suffix() -> None:
    dt = datetime(2026, 8, 10, 9, 0, 0, 123456, tzinfo=UTC)
    assert format_utc(dt) == "2026-08-10T09:00:00.123Z"


def test_format_utc_truncates_microseconds_to_ms() -> None:
    dt = datetime(2026, 8, 10, 9, 0, 0, 999999, tzinfo=UTC)
    assert format_utc(dt) == "2026-08-10T09:00:00.999Z"


def test_format_utc_converts_offset_to_utc() -> None:
    from datetime import timedelta, timezone

    dt = datetime(2026, 8, 10, 17, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert format_utc(dt) == "2026-08-10T09:00:00.000Z"


def test_format_utc_naive_raises() -> None:
    with pytest.raises(ValueError, match="naive"):
        format_utc(datetime(2026, 8, 10, 9, 0, 0))  # noqa: DTZ001 — 有意构造 naive datetime
