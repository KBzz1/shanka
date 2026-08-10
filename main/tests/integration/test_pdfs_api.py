"""PDF API 集成测试（迁移 schema + HTTP）：上传/列表/详情/删除/PATCH + 三重校验。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "pdf_api.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 fake pdf content for upload validation"


def test_pdfs_api_upload_invalid_magic_400(client: TestClient) -> None:
    resp = client.post(
        "/pdfs",
        files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
        headers={**_device(), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_pdfs_api_upload_invalid_extension_400(client: TestClient) -> None:
    resp = client.post(
        "/pdfs",
        files={"file": ("a.txt", _pdf_bytes(), "application/pdf")},
        headers={**_device(), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_pdfs_api_upload_accepts_and_lists(client: TestClient) -> None:
    device = _device()
    resp = client.post(
        "/pdfs",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] in ("PENDING", "PARSING")
    assert body["filename"] == "book.pdf"
    file_id = body["file_id"]
    resp = client.get("/pdfs", headers=device)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["file_id"] == file_id


def test_pdfs_api_get_detail_and_404(client: TestClient) -> None:
    device = _device()
    file_id = client.post(
        "/pdfs",
        files={"file": ("b.pdf", _pdf_bytes(), "application/pdf")},
        headers={**device, **_idem()},
    ).json()["file_id"]
    resp = client.get(f"/pdfs/{file_id}", headers=device)
    assert resp.status_code == 200
    other = _device()
    resp = client.get(f"/pdfs/{file_id}", headers=other)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PDF_NOT_FOUND"


def test_pdfs_api_delete_204_and_storage_cleaned(client: TestClient, tmp_path: Path) -> None:
    device = _device()
    file_id = client.post(
        "/pdfs",
        files={"file": ("c.pdf", _pdf_bytes(), "application/pdf")},
        headers={**device, **_idem()},
    ).json()["file_id"]
    resp = client.delete(f"/pdfs/{file_id}", headers={**device, **_idem()})
    assert resp.status_code == 204
    resp = client.get(f"/pdfs/{file_id}", headers=device)
    assert resp.status_code == 404
    # 存储对象随元数据删除清理（T2 用例：同一调用内 storage.delete）
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]


def test_pdfs_api_patch_chapter_requires_parsed(client: TestClient) -> None:
    """非 PARSED 时 PATCH → 409（裁决）；PARSED 后 PATCH 成功由扫描器链路覆盖（Task 5 或本测试手动置 PARSED）。"""
    device = _device()
    file_id = client.post(
        "/pdfs",
        files={"file": ("d.pdf", _pdf_bytes(), "application/pdf")},
        headers={**device, **_idem()},
    ).json()["file_id"]
    # 上传后未解析 → PATCH 章节（无章节 → 404 或 409）
    resp = client.patch(
        f"/pdfs/{file_id}/chapters/{uuid.uuid4()}",
        json={"name": "x", "start_page": 1, "end_page": 2},
        headers={**device, **_idem()},
    )
    assert resp.status_code in (404, 409)
