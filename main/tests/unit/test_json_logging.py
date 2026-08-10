"""infra.logging JSON 结构化日志单元测试（structure-contract 8.1）。"""

import json
import logging

from infra.logging import JSONFormatter


def _capture(record: logging.LogRecord) -> dict[str, object]:
    formatter = JSONFormatter()
    line = formatter.format(record)
    return json.loads(line)  # type: ignore[no-any-return]


def test_json_logging_single_line_with_contract_fields() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request ok",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-123"
    record.device_id = "dev-1"
    data = _capture(record)
    assert set(data) >= {
        "timestamp",
        "level",
        "request_id",
        "device_id",
        "task_id",
        "batch_id",
        "error_code",
        "message",
    }
    assert data["message"] == "request ok"
    assert data["level"] == "INFO"
    assert data["request_id"] == "req-123"


def test_json_logging_extra_attributes_flat_keys() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="rate limited",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-1"
    record.method = "POST"
    record.path = "/v1/decks"
    record.status = 429
    record.duration_ms = 12
    data = _capture(record)
    assert data["method"] == "POST"
    assert data["path"] == "/v1/decks"
    assert data["status"] == 429
    assert data["duration_ms"] == 12
    assert data["message"] == "rate limited"
