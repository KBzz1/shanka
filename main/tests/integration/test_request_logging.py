"""request_id + JSON 请求日志集成测试（structure-contract 8.1）。"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from infra.logging import JSONFormatter


class _CapturingHandler(logging.Handler):
    """把 JSON 格式化后的日志行解析进内存列表（mypy strict 下替代 emit 赋值写法）。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        super().__init__()
        self._records = records
        self.setFormatter(JSONFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(json.loads(self.format(record)))


@pytest.fixture
def captured_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[list[dict[str, object]]]:
    """把请求日志中间件 logger 的 JSON 行捕获到内存列表。"""
    records: list[dict[str, object]] = []
    handler = _CapturingHandler(records)
    logger = logging.getLogger("app.middleware.logging")
    # alembic env.py 的 fileConfig(disable_existing_loggers) 会禁用未配置 logger，
    # 且 setLevel 不会重置 disabled——测试侧显式重新启用，保证捕获稳定。
    monkeypatch.setattr(logger, "disabled", False)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)


def test_request_id_present_in_response_header(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")


def test_request_logging_emits_json_line(
    client: TestClient, captured_logs: list[dict[str, object]]
) -> None:
    client.get("/healthz")
    assert len(captured_logs) >= 1
    entry = captured_logs[0]
    assert set(entry) >= {
        "timestamp",
        "level",
        "request_id",
        "device_id",
        "task_id",
        "batch_id",
        "error_code",
        "message",
    }
    assert entry["level"] == "INFO"
    assert entry["message"]
