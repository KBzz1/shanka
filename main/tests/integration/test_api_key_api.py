"""API Key API 集成测试（迁移 schema + HTTP + mock transport 注入）。

测试注入策略（plan 决策 a）：monkeypatch handler 消费的 DeepSeekClient 为 FakeClient——
validate_key 返回可控状态（模块级 _FAKE_STATUS/_FAKE_ERROR），close() 记录 no-op，不触网。
注入点必须为 app.api.api_key（handler 经 `from infra.llm.deepseek import DeepSeekClient`
导入，import 期绑定：patch 源模块属性不影响消费模块已绑定名）。
devices 行由 F1 设备中间件自动建立（HTTP 流），无需显式补种。
加密密钥缺失（500）与 GET 不解密场景用无密钥 Settings 的独立 client fixture。
"""

import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.main import create_app
from infra.db.session import create_db_engine
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

_TEST_KEY_HEX = "aa" * 32

# FakeClient 行为开关：各测试经 monkeypatch 设置（fixture 自动还原）
_FAKE_STATUS: str = "AVAILABLE"
_FAKE_ERROR: AppError | None = None


class FakeClient:
    """mock transport：validate_key 返回模块级可控状态；close() no-op 并记录。

    last/validate_calls 为跨请求观测点（close 生命周期、幂等重放不重校验断言）。
    """

    last: "FakeClient | None" = None
    validate_calls = 0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.closed = False
        FakeClient.last = self

    def close(self) -> None:
        self.closed = True

    def validate_key(self, api_key: str) -> str:
        FakeClient.validate_calls += 1
        if _FAKE_ERROR is not None:
            raise _FAKE_ERROR
        return _FAKE_STATUS


def _make_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, encryption_key: str | None
) -> Iterator[TestClient]:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "api.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 双头窗口：Bearer 注册请求计入 IP 维度（连发 >5 req/s），显式调高隔离,
        api_key_encryption_key=encryption_key,
    )
    monkeypatch.setattr(FakeClient, "validate_calls", 0)
    monkeypatch.setattr(FakeClient, "last", None)
    monkeypatch.setattr("app.api.api_key.DeepSeekClient", FakeClient)
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """带加密密钥的 TestClient（PUT 落库路径）。"""
    yield from _make_client(tmp_path, monkeypatch, encryption_key=_TEST_KEY_HEX)


@pytest.fixture
def client_without_encryption_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """无加密密钥（配置错误）的 TestClient：PUT → 500，GET 仍可用（不解密）。"""
    yield from _make_client(tmp_path, monkeypatch, encryption_key=None)


def _device(client: TestClient) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ 随机 X-Device-ID（v2.1 device 隔离语义保持）。"""
    return {**auth_headers(client), "X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _api_key_rows(db_path: Path) -> list[tuple[str, str]]:
    """api_keys 表直接观测（落库/不落库 + 密文不含明文断言）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT encrypted_key, masked_key FROM api_keys")).all()
    return [(str(row[0]), str(row[1])) for row in rows]


def test_api_key_put_available_saves_and_status_returns(client: TestClient, tmp_path: Path) -> None:
    """PUT 校验通过 → 200 AVAILABLE + 脱敏 + updated_at；落库；GET status 一致且无明文。"""
    device = _device(client)
    resp = client.put("/api-key", json={"api_key": "sk-test123456"}, headers={**device, **_idem()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AVAILABLE"
    assert body["masked_key"] == "sk-****3456"
    assert body["updated_at"]
    assert "sk-test123456" not in str(body)  # 红线 4：响应不得引用明文

    rows = _api_key_rows(tmp_path / "api.db")
    assert len(rows) == 1
    encrypted, masked = rows[0]
    assert "sk-test123456" not in encrypted  # 密文不含明文
    assert masked == "sk-****3456"

    resp = client.get("/api-key/status", headers=device)
    assert resp.status_code == 200
    status = resp.json()
    assert status["status"] == "AVAILABLE"
    assert status["masked_key"] == "sk-****3456"
    assert status["updated_at"] == body["updated_at"]


def test_api_key_put_invalid_returns_status_and_not_saved(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """校验失败 → 200 INVALID（不落库不覆盖）；GET status → UNKNOWN。"""
    monkeypatch.setattr(sys.modules[__name__], "_FAKE_STATUS", "INVALID")
    device = _device(client)
    resp = client.put("/api-key", json={"api_key": "sk-bad"}, headers={**device, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "INVALID"
    assert resp.json()["masked_key"] == "sk-****-bad"  # masked(): sk-**** + 末 4 位
    assert _api_key_rows(tmp_path / "api.db") == []  # 不落库

    resp = client.get("/api-key/status", headers=device)
    assert resp.status_code == 200
    assert resp.json()["status"] == "UNKNOWN"


def test_api_key_put_insufficient_returns_status_and_not_saved(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "_FAKE_STATUS", "INSUFFICIENT_BALANCE")
    device = _device(client)
    resp = client.put("/api-key", json={"api_key": "sk-low"}, headers={**device, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "INSUFFICIENT_BALANCE"
    assert _api_key_rows(tmp_path / "api.db") == []


def test_api_key_put_upstream_unavailable_502_and_client_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游不可用 → 502 API_KEY_UNAVAILABLE；biz 抛异常路径 client 仍 close（try/finally）。"""
    monkeypatch.setattr(
        sys.modules[__name__],
        "_FAKE_ERROR",
        AppError(ErrorCode.API_KEY_UNAVAILABLE, "DeepSeek 上游不可用"),
    )
    device = _device(client)
    resp = client.put("/api-key", json={"api_key": "sk-x"}, headers={**device, **_idem()})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "API_KEY_UNAVAILABLE"
    assert FakeClient.last is not None
    assert FakeClient.last.closed is True  # finally close：异常路径也释放


def test_api_key_put_missing_idempotency_key_400(client: TestClient) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）：缺失 → 400 VALIDATION_ERROR。"""
    resp = client.put("/api-key", json={"api_key": "sk-x"}, headers=_device(client))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_api_key_put_missing_api_key_field_400(client: TestClient) -> None:
    """请求体缺 api_key（Pydantic 必填）→ 400 VALIDATION_ERROR（F1 统一校验响应）。"""
    resp = client.put("/api-key", json={}, headers={**_device(client), **_idem()})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_api_key_put_empty_api_key_400(client: TestClient) -> None:
    """请求体 api_key 为空串（min_length=1）→ 400 VALIDATION_ERROR，不触达校验/落库。"""
    resp = client.put("/api-key", json={"api_key": ""}, headers={**_device(client), **_idem()})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert FakeClient.validate_calls == 0


def test_api_key_put_idempotent_replay_does_not_revalidate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 key 同 body 重放：返回首次响应且校验不重复（重放时 fn 不执行）。"""
    monkeypatch.setattr(sys.modules[__name__], "_FAKE_STATUS", "AVAILABLE")
    device = _device(client)
    headers = {**device, **_idem()}
    resp1 = client.put("/api-key", json={"api_key": "sk-test123456"}, headers=headers)
    assert resp1.status_code == 200
    assert FakeClient.validate_calls == 1
    resp2 = client.put("/api-key", json={"api_key": "sk-test123456"}, headers=headers)
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()  # 原响应原样重放
    assert FakeClient.validate_calls == 1  # 重放未重新校验


def test_api_key_put_idempotency_conflict_409(client: TestClient) -> None:
    """同 key 异 body → 409 IDEMPOTENCY_CONFLICT。"""
    device = _device(client)
    headers = {**device, **_idem()}
    resp = client.put("/api-key", json={"api_key": "sk-first"}, headers=headers)
    assert resp.status_code == 200
    resp = client.put("/api-key", json={"api_key": "sk-other"}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_api_key_status_unknown_when_not_saved(client: TestClient) -> None:
    """未保存 Key → 200 UNKNOWN + masked_key 空串 + updated_at null（openapi required 字段存在）。"""
    resp = client.get("/api-key/status", headers=_device(client))
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "UNKNOWN", "masked_key": "", "updated_at": None}


def test_api_key_put_missing_encryption_key_500(client_without_encryption_key: TestClient) -> None:
    """加密密钥缺失（配置错误）→ 500 INTERNAL_ERROR（_require_encryption_key）。"""
    resp = client_without_encryption_key.put(
        "/api-key",
        json={"api_key": "sk-x"},
        headers={**_device(client_without_encryption_key), **_idem()},
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


def test_api_key_status_without_encryption_key_still_works(
    client_without_encryption_key: TestClient,
) -> None:
    """GET status 不解密：无加密密钥时传空 bytes 无碍 → 200 UNKNOWN。"""
    resp = client_without_encryption_key.get(
        "/api-key/status", headers=_device(client_without_encryption_key)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "UNKNOWN"
