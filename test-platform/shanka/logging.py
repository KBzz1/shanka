"""JSON Lines 事件日志:run_id 上下文/字段规范/脱敏(对齐后端 app.log 风格)。

事件字段:必选 timestamp/level/run_id/message;请求事件另含
suite/scenario/step/request_id/device_id/method/path/status/duration_ms/error_code。
脱敏纪律:调用方不得把 API Key 明文、设备 ID 混入 message 或附加字段。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

_lock = threading.Lock()
_run_id = ""
_suite = ""
_scenario = ""
_device_id = ""
_file: TextIO | None = None
_console = False


def init_logger(run_id: str, log_path: Path | None = None, console: bool = False) -> None:
    """全局初始化一次;log_path 为 None 时仅 console。追加式,不截断;目录自动创建。"""
    global _run_id, _file, _console
    with _lock:
        _run_id = run_id
        _console = console
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _file = open(log_path, "a", encoding="utf-8")


def set_context(*, suite: str, scenario: str, device_id: str) -> None:
    global _suite, _scenario, _device_id
    with _lock:
        _suite, _scenario, _device_id = suite, scenario, device_id


def event(level: str, message: str, **fields: Any) -> None:
    row: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "level": level,
        "run_id": _run_id,
        "message": message,
    }
    if _suite:
        row["suite"] = _suite
    if _scenario:
        row["scenario"] = _scenario
    if _device_id:
        row["device_id"] = _device_id
    for k, v in fields.items():
        row[k] = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
    text = json.dumps(row, ensure_ascii=False)
    with _lock:
        if _file is not None:
            _file.write(text + "\n")
            _file.flush()
        if _console:
            print(text)
