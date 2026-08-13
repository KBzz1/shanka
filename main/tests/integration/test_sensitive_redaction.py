"""敏感信息脱敏（DESIGN §4.5 / 红线 4：Authorization/密码/token 不进日志）。

- Authorization 头（含 Bearer token）绝不记录：logging 中间件不记 headers
  （只记 method/path/status/duration_ms + request_id/user_id 上下文）。
- /auth/register /auth/login /api-key 为敏感路径：不额外记录任何 body/header
  （BodyCapture 只提供幂等 body hash，不落 body 原文）。
- 日志身份字段为 user_id（principal 存在时），device_id 已随 X-Device-ID 退出。
"""

import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from infra.logging import JSONFormatter
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


class _CapturingHandler(logging.Handler):
    """把 JSON 格式化后的日志行解析进内存列表（同 test_request_logging.py 定式）。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        super().__init__()
        self._records = records
        self.setFormatter(JSONFormatter())

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(json.loads(self.format(record)))


@pytest.fixture
def captured_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[dict[str, object]]]:
    """捕获请求日志中间件 logger 的 JSON 行（身份字段断言走生产格式）。"""
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


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（auth 中间件 → auth_sessions 表）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "redaction.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 流程用例快速连发，隔离 IP 5 req/s 总闸门
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_authorization_header_never_logged(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    captured_logs: list[dict[str, object]],
) -> None:
    """Authorization 头（含 Bearer token）不进日志（DESIGN §4.5）。"""
    h = auth_headers(client)
    with caplog.at_level(logging.INFO):
        client.get("/decks", headers=h)
    assert "Bearer" not in caplog.text
    assert "secret-pass-1" not in caplog.text
    # JSON 日志行同样不得出现（记录字段不含任何 header）
    assert all("Bearer" not in json.dumps(record) for record in captured_logs)


def test_login_failure_does_not_log_password(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """login 失败不记密码：唯一 sentinel 密码（仅测试代码字面量）注册后登录失败，
    日志全文不含 sentinel。"""
    sentinel = f"s3nt1n3l-{uuid.uuid4().hex[:8]}"
    r = client.post("/auth/register", json={"username": "sentineluser", "password": sentinel})
    assert r.status_code == 201
    with caplog.at_level(logging.INFO):
        r = client.post("/auth/login", json={"username": "nosuchuser", "password": sentinel})
    assert r.status_code == 401
    assert sentinel not in caplog.text


def test_log_identity_field_is_user_id(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    captured_logs: list[dict[str, object]],
) -> None:
    """访问日志身份字段 = user_id（principal 存在时）；device_id 已退出。"""
    h = auth_headers(client)
    with caplog.at_level(logging.INFO):
        r = client.get("/decks", headers=h)
    assert r.status_code == 200
    entry = captured_logs[-1]  # 末条 = GET /decks 的请求完成行
    assert "user_id" in entry
    assert entry["user_id"]  # 已认证请求：principal.user_id 非空
    assert "device_id" not in entry
    assert all("device_id" not in record for record in captured_logs)
