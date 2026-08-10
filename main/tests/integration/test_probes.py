"""探针集成测试（structure-contract 8.2）：healthz 存活、readyz DB+存储真实检查。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_probes_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_probes_readyz_ok_creates_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ready.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["checks"] == {"database": "ok", "storage": "ok"}
    assert db_path.exists()  # 空测试库创建


def test_probes_readyz_db_unavailable_returns_503(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:////nonexistent-dir/app.db", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "error"


def test_probes_readyz_storage_unavailable_returns_503(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-dir", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'ok.db'}", storage_path=blocker / "sub"
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"] == "error"


def test_probes_healthz_alive_even_when_db_down(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:////nonexistent-dir/app.db", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
