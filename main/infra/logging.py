"""JSON 结构化日志（structure-contract 8.1）：单行 JSON，字段固定。

字段：timestamp(ISO 8601 UTC) / level / request_id / device_id / task_id /
batch_id / error_code / message；记录上的附加属性（method/path/status/
duration_ms）以扁平键输出。敏感红线（1.5/7.1）：API Key、完整 PDF 内容、
完整 Prompt 不落日志——请求日志不记录任何请求体。
"""

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from infra.db.session import format_utc

# 契约 8.1 级别字样为 INFO/WARN/ERROR：WARNING 映射为 WARN，其余透传。
_LEVEL_ALIASES = {"WARNING": "WARN"}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": format_utc(datetime.now(UTC)),
            "level": _LEVEL_ALIASES.get(record.levelname, record.levelname),
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


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """装配根 logger：stderr（默认）+ 可选滚动文件落盘（log_dir/app.log）。

    落盘失败（如只读环境）静默降级 stderr，不阻断启动；日志内容遵守契约 8.1
    红线（不记录请求体、API Key、PDF 内容）。
    """
    root = logging.getLogger()
    root.handlers.clear()
    stream = logging.StreamHandler()
    stream.setFormatter(JSONFormatter())
    root.addHandler(stream)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(JSONFormatter())
            root.addHandler(file_handler)
        except OSError:
            pass  # 日志落盘失败不阻断启动（降级 stderr）
    root.setLevel(level.upper())
