"""JSON 结构化日志（structure-contract 8.1）：单行 JSON，字段固定。

字段：timestamp(ISO 8601 UTC) / level / request_id / device_id / task_id /
batch_id / error_code / message；记录上的附加属性（method/path/status/
duration_ms）以扁平键输出。敏感红线（1.5/7.1）：API Key、完整 PDF 内容、
完整 Prompt 不落日志——请求日志不记录任何请求体。
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

from infra.db.session import format_utc

_CONTRACT_FIELDS = (
    "timestamp",
    "level",
    "request_id",
    "device_id",
    "task_id",
    "batch_id",
    "error_code",
    "message",
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": format_utc(datetime.now(UTC)),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", ""),
            "device_id": getattr(record, "device_id", ""),
            "task_id": getattr(record, "task_id", ""),
            "batch_id": getattr(record, "batch_id", ""),
            "error_code": getattr(record, "error_code", ""),
            "message": record.getMessage(),
        }
        for attr in ("method", "path", "status", "duration_ms"):
            if hasattr(record, attr):
                data[attr] = getattr(record, attr)
        if record.exc_info:
            data["message"] = f"{record.getMessage()} | {self.formatException(record.exc_info)}"
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
