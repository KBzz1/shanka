"""infra.logging JSON 结构化日志单元测试（structure-contract 8.1）。"""

import json
import logging
import logging.handlers
from pathlib import Path

from infra.logging import JSONFormatter, setup_logging


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
    record.user_id = "u-1"
    data = _capture(record)
    assert set(data) >= {
        "timestamp",
        "level",
        "request_id",
        "user_id",
        "task_id",
        "batch_id",
        "error_code",
        "message",
    }
    assert data["message"] == "request ok"
    assert data["level"] == "INFO"
    assert data["request_id"] == "req-123"
    assert data["user_id"] == "u-1"


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
    assert data["level"] == "WARN"  # 契约 8.1 级别字样：WARNING → WARN


def test_setup_logging_rotating_file(tmp_path: Path) -> None:
    """setup_logging(log_dir) 落盘滚动文件（2026-08-11 前端联调：后端日志无落盘无法对照时间窗）。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        log_dir = tmp_path / "logs"
        setup_logging("INFO", log_dir)
        logging.getLogger("test").info("落盘消息")
        for h in root.handlers:
            if isinstance(h, logging.handlers.RotatingFileHandler):
                h.flush()
        files = list(log_dir.glob("app.log*"))
        assert files, "未生成 app.log"
        content = files[0].read_text(encoding="utf-8")
        assert "落盘消息" in content
        # JSON 单行 + 契约字段
        line = json.loads(content.strip().splitlines()[0])
        assert line["level"] == "INFO"
        assert line["message"] == "落盘消息"
    finally:
        root.handlers.clear()
        for h in saved_handlers:
            root.addHandler(h)
