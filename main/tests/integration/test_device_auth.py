"""X-Device-ID 鉴权集成测试（structure-contract 1.1；database-design 2.1）。"""

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _new_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'devices.db'}", storage_path=tmp_path / "storage"
    )
    # 双头过渡窗口（P4-2）：Bearer 经 auth 中间件 → 需 users/auth_sessions 表
    # （create_all 与 first_seen 测试同款；本中间件在 P4-4 整体移除，届时随测试收敛）。
    from infra.db.models import Base
    from infra.db.session import create_db_engine

    Base.metadata.create_all(create_db_engine(settings.database_url))
    return TestClient(create_app(settings))


def test_device_auth_missing_header_returns_401(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        headers = auth_headers(client)
        headers.pop("X-Device-ID")  # 只缺设备头：Bearer 通过后仍由设备中间件裁决
        resp = client.get("/v1/decks", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "DEVICE_ID_REQUIRED"
    assert resp.json()["error"]["localization_key"] == "error.device_id_required"


def test_device_auth_invalid_format_returns_401(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        resp = client.get(
            "/v1/decks", headers={**auth_headers(client), "X-Device-ID": "not-a-uuid"}
        )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "DEVICE_ID_INVALID"


def test_device_auth_accepts_valid_uuid(tmp_path: Path) -> None:
    import uuid

    device_id = str(uuid.uuid4())
    with _new_client(tmp_path) as client:
        resp = client.get("/v1/decks", headers={**auth_headers(client), "X-Device-ID": device_id})
    assert resp.status_code == 404  # 无路由 → 404；鉴权已通过


def test_device_auth_first_seen_registers_device_row(tmp_path: Path) -> None:
    import uuid

    from infra.db.session import create_db_engine

    device_id = str(uuid.uuid4())
    db_path = tmp_path / "devices.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        # 建表：设备注册依赖 F1 迁移 schema——TestClient 启动不自动迁移，此处用 Base 建表
        from infra.db.models import Base

        engine = create_db_engine(settings.database_url)
        Base.metadata.create_all(engine)
        client.get("/healthz")  # 先触发一次连接
        resp = client.get(
            "/v1/not-exist", headers={**auth_headers(client), "X-Device-ID": device_id}
        )
    assert resp.status_code == 404
    with create_db_engine(f"sqlite:///{db_path}").connect() as conn:
        row = conn.execute(text("SELECT device_id, created_at FROM devices")).fetchall()
    assert len(row) == 1
    assert row[0][0] == device_id
    assert row[0][1]  # created_at 非空


def test_device_auth_probes_exempt(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        # 接口文档豁免（无设备上下文可在线拉取，前端对接用）
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
