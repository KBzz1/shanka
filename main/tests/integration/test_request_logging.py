"""request_id + JSON 请求日志集成测试（structure-contract 8.1）。"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra.logging import JSONFormatter
from tests.conftest import auth_headers


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
    resp = client.get("/healthz")
    assert len(captured_logs) >= 1
    entry = captured_logs[0]
    assert set(entry) >= {
        "timestamp",
        "level",
        "request_id",
        "user_id",  # P4-6：身份字段切 user_id（匿名请求为空串）；device_id 已退出
        "task_id",
        "batch_id",
        "error_code",
        "message",
    }
    assert entry["level"] == "INFO"
    assert entry["message"]
    # 日志 request_id 与响应头 X-Request-ID 一致（同一请求贯穿）
    assert entry["request_id"]
    assert entry["request_id"] == resp.headers.get("X-Request-ID")


def test_request_logging_error_path_logs_internal_error(
    app: FastAPI, captured_logs: list[dict[str, object]], tmp_path: Path
) -> None:
    def _boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/boom", _boom, methods=["GET"])
    # raise_server_exceptions=False：Starlette 对未处理异常先发 500 再重抛
    # （ServerErrorMiddleware 语义），测试关注 500 与 ERROR 日志而非重抛。
    # P4-4 起仅 Bearer：auth 中间件 → auth_sessions 查询需表结构。用 create_all 而非
    # alembic upgrade：alembic env.py 的 fileConfig 会再次禁用 captured_logs 目标 logger。
    from infra.db.models import Base

    Base.metadata.create_all(app.state.engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom", headers=auth_headers(client))
    assert resp.status_code == 500
    assert len(captured_logs) >= 1
    # auth_headers 注册请求先产生 INFO 访问日志；取末条（/boom 的 ERROR 行）
    entry = captured_logs[-1]
    assert entry["level"] == "ERROR"
    assert entry["error_code"] == "INTERNAL_ERROR"
    assert "boom" in str(entry["message"])
