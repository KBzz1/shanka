"""JSON Lines 事件日志:run_id 上下文/字段规范/脱敏(对齐后端 app.log 风格)。

事件字段:必选 timestamp/level/run_id/message;请求事件另含
suite/scenario/step/request_id/user_id/method/path/status/duration_ms/error_code。
账号化(DESIGN 4.5/8.1):日志身份字段为 user_id(不再有 device_id)。
脱敏纪律(红线 4):敏感字段统一自动脱敏——Authorization -> Bearer ***,password/token/
api_key 等 -> ***;调用方不得把明文凭据混入 message(register/login 等敏感路径由
client 直接不落事件)。
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
_user_id = ""
_file: TextIO | None = None
_console = False

_SENSITIVE_FIELDS = frozenset({"authorization", "token", "access_token", "password", "api_key", "secret"})
_MASK = "***"


def redact_field(key: str, value: Any) -> Any:
    """字段级脱敏(client 与 logging 统一):Authorization -> Bearer ***;其余敏感字段 -> ***。"""
    if not isinstance(value, str) or not value:
        return value
    k = key.lower()
    if k == "authorization" and value.lower().startswith("bearer "):
        return "Bearer ***"
    if k in _SENSITIVE_FIELDS or any(s in k for s in ("token", "password", "secret")):
        return _MASK
    return value


def init_logger(run_id: str, log_path: Path | None = None, console: bool = False) -> None:
    """全局初始化一次;log_path 为 None 时仅 console。追加式,不截断;目录自动创建。"""
    global _run_id, _file, _console
    with _lock:
        _run_id = run_id
        _console = console
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _file = open(log_path, "a", encoding="utf-8")


def set_context(*, suite: str, scenario: str, user_id: str) -> None:
    global _suite, _scenario, _user_id
    with _lock:
        _suite, _scenario, _user_id = suite, scenario, user_id


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
    if _user_id:
        row["user_id"] = _user_id
    for k, v in fields.items():
        v = v if isinstance(v, (str, int, float, bool, type(None))) else str(v)
        row[k] = redact_field(k, v)
    text = json.dumps(row, ensure_ascii=False)
    with _lock:
        if _file is not None:
            _file.write(text + "\n")
            _file.flush()
        if _console:
            print(text)
