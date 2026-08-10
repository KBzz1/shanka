"""验收测试：AC-11 API Key 联调（PRD；迁移 schema + HTTP + FakeClient 注入）。

映射：
- AC-11-a 验证并保存返回状态（AVAILABLE/INVALID/INSUFFICIENT_BALANCE/通用失败）且前端可展示
- AC-11-b Key 加密保存，不出现在服务端日志/任务详情/分析数据/接口响应
- AC-11-c 前端仅展示状态不展示完整 Key

FakeClient 注入（plan 决策 a / T4 模式）：monkeypatch 注入点必须为 app.api.api_key
（handler 经 `from infra.llm.deepseek import DeepSeekClient` 导入，import 期绑定：
patch 源模块属性不影响消费模块已绑定名）。DB 密文无明文由 service/集成测试覆盖，
验收层补响应/日志无明文断言（红线 4）。
"""

import json
import logging
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

_TEST_KEY_HEX = "aa" * 32


class FakeClient:
    """测试替身：不触网，状态可控。"""

    def __init__(self, settings: Settings, transport: object | None = None) -> None:
        self.validate_result = "AVAILABLE"

    def validate_key(self, api_key: str) -> str:
        return self.validate_result

    def close(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    monkeypatch.setattr("app.api.api_key.DeepSeekClient", FakeClient)
    db_path = tmp_path / "ac11.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
        api_key_encryption_key=_TEST_KEY_HEX,
    )
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_acceptance_ac11_save_and_status(client: TestClient) -> None:
    """AC-11-1：验证并保存返回状态（AVAILABLE），GET status 返回脱敏标识。"""
    device = _device()
    resp = client.put(
        "/api-key", json={"api_key": "sk-ac11-secret-value"}, headers={**device, **_idem()}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AVAILABLE"
    assert "sk-ac11-secret-value" not in json.dumps(body)  # 响应无明文
    assert body["masked_key"] == "sk-****alue"
    resp = client.get("/api-key/status", headers=device)
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"
    assert "sk-ac11-secret-value" not in json.dumps(resp.json())


def test_acceptance_ac11_unknown_when_not_saved(client: TestClient) -> None:
    """AC-11-b/c：未保存 Key → 200 UNKNOWN + masked_key 空串（无明文可展示）。"""
    resp = client.get("/api-key/status", headers=_device())
    assert resp.status_code == 200
    assert resp.json()["status"] == "UNKNOWN"
    assert resp.json()["masked_key"] == ""


def test_acceptance_ac11_no_plaintext_in_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-11/AC-08：请求日志无明文 Key（红线 4：任何日志不得引用明文）。"""
    device = _device()
    # alembic fileConfig（migrations/env.py，disable_existing_loggers 默认 True）在 fixture
    # 迁移时禁用既有 logger → 临时恢复请求日志中间件 logger，使断言真实覆盖请求日志
    monkeypatch.setattr(logging.getLogger("app.middleware.logging"), "disabled", False)
    with caplog.at_level(logging.INFO):
        put = client.put(
            "/api-key", json={"api_key": "sk-secret-log-check"}, headers={**device, **_idem()}
        )
        status = client.get("/api-key/status", headers=device)
    assert put.status_code == 200
    assert status.status_code == 200
    combined = caplog.text
    # 请求日志存在（LoggingMiddleware INFO "request complete"：method/path/status 元数据，不含 body）
    assert "request complete" in combined
    assert "sk-secret-log-check" not in combined
