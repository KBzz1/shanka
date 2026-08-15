"""PDF API 集成测试（迁移 schema + HTTP）：上传/列表/详情/删除/PATCH + 三重校验。"""

import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


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


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    from sqlalchemy import text

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    engine.dispose()
    assert row is not None
    return str(row)


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 fake pdf content for upload validation"


def _seed_parsed_pdf(db_path: Path, user_id: str) -> tuple[str, str]:
    """直接种 PARSED PDF + 章节（PATCH 部分更新成功路径；完整解析链路由 Task 5 验收覆盖）。

    前置：users 行须已存在（注册端点建立），user_id 按用户名查 users 表取得。
    """
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Chapter, PdfFile
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    pdf_id, chapter_id = str(uuid.uuid4()), str(uuid.uuid4())
    with factory() as session:
        session.add(
            PdfFile(
                file_id=pdf_id,
                user_id=user_id,
                filename="seeded.pdf",
                storage_key="a" * 32,
                size_bytes=100,
                status="PARSED",
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()  # 无 relationship 时 UoW 不保证插入顺序——先落 pdf_files 行再插章节
        session.add(
            Chapter(chapter_id=chapter_id, file_id=pdf_id, name="旧名", start_page=1, end_page=10)
        )
        session.commit()
    engine.dispose()
    return pdf_id, chapter_id


def test_pdfs_api_upload_invalid_magic_400(client: TestClient) -> None:
    resp = client.post(
        "/pdfs",
        files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_pdfs_api_upload_invalid_extension_400(client: TestClient) -> None:
    resp = client.post(
        "/pdfs",
        files={"file": ("a.txt", _pdf_bytes(), "application/pdf")},
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_pdfs_api_upload_content_length_precheck_400(client: TestClient) -> None:
    """final review I-1：伪造超大 Content-Length 头（110MB > 100MB 上限）+ 合法小 body → 400
    PDF_UPLOAD_INVALID；BodyCaptureMiddleware 在读 body 前按头预检拒绝（不读 body）。"""
    resp = client.post(
        "/pdfs",
        files={"file": ("big.pdf", _pdf_bytes(), "application/pdf")},
        headers={**_user(client), **_idem(), "Content-Length": "110000000"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "PDF_UPLOAD_INVALID"
    assert body["error"]["localization_key"] == "error.pdf_upload_invalid"


def test_pdfs_api_upload_accepts_and_lists(client: TestClient) -> None:
    user = _user(client)
    resp = client.post(
        "/pdfs",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] in ("PENDING", "PARSING")
    assert body["filename"] == "book.pdf"
    file_id = body["file_id"]
    resp = client.get("/pdfs", headers=user)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["file_id"] == file_id


def test_pdfs_api_get_detail_and_404(client: TestClient) -> None:
    user = _user(client)
    file_id = client.post(
        "/pdfs",
        files={"file": ("b.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["file_id"]
    resp = client.get(f"/pdfs/{file_id}", headers=user)
    assert resp.status_code == 200
    other = _user(client, "user2", "pass-2222")
    resp = client.get(f"/pdfs/{file_id}", headers=other)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PDF_NOT_FOUND"


def test_pdfs_api_delete_204_and_storage_cleaned(client: TestClient, tmp_path: Path) -> None:
    """兼容删除委托项目删除（V2.5 语义，6.2）：解析中项目不可删（V25-GEN-FR-09 状态保护）；
    解析失败（PARSE_FAILED）后可删 → 204 + 存储对象随元数据清理。"""
    user = _user(client)
    file_id = client.post(
        "/pdfs",
        files={"file": ("c.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["file_id"]
    # 上传即建项目（兼容委托），解析中（PENDING）→ 409 PROJECT_STATE_CONFLICT（不再直接删 PDF）
    resp = client.delete(f"/pdfs/{file_id}", headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "PROJECT_STATE_CONFLICT"
    # 触发解析（fake PDF 无文本层 → FAILED → 项目 PARSE_FAILED）→ 可删除
    from services.pdf.scanner import scan_once

    app = cast(FastAPI, client.app)
    scan_once(app.state.session_factory, storage=app.state.storage)
    resp = client.delete(f"/pdfs/{file_id}", headers={**user, **_idem()})
    assert resp.status_code == 204
    resp = client.get(f"/pdfs/{file_id}", headers=user)
    assert resp.status_code == 404
    # 存储对象随元数据删除清理（T2 用例：同一调用内 storage.delete）
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]


def test_pdfs_api_patch_chapter_requires_parsed(client: TestClient) -> None:
    """非 PARSED 时 PATCH → 409（裁决）；PARSED 后 PATCH 成功由扫描器链路覆盖（Task 5 或本测试手动置 PARSED）。"""
    user = _user(client)
    file_id = client.post(
        "/pdfs",
        files={"file": ("d.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["file_id"]
    # 上传后未解析 → PATCH 章节（无章节 → 404 或 409）
    resp = client.patch(
        f"/pdfs/{file_id}/chapters/{uuid.uuid4()}",
        json={"name": "x", "start_page": 1, "end_page": 2},
        headers={**user, **_idem()},
    )
    assert resp.status_code in (404, 409)


def test_pdfs_api_patch_chapter_partial_name_only(client: TestClient, tmp_path: Path) -> None:
    """部分更新（fix round 1）：只传 name → 200，start/end 保持。"""
    user = _user(client)
    assert client.get("/pdfs", headers=user).status_code == 200  # 注册设备行（FK 前置）
    pdf_id, chapter_id = _seed_parsed_pdf(
        tmp_path / "pdf_api.db", _user_id(tmp_path / "pdf_api.db")
    )
    resp = client.patch(
        f"/pdfs/{pdf_id}/chapters/{chapter_id}",
        json={"name": "改名"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "改名"
    assert body["start_page"] == 1 and body["end_page"] == 10


def test_pdfs_api_patch_chapter_partial_start_page_only(client: TestClient, tmp_path: Path) -> None:
    """部分更新（fix round 1）：只传 start_page → 200，end/name 保持。"""
    user = _user(client)
    assert client.get("/pdfs", headers=user).status_code == 200  # 注册设备行（FK 前置）
    pdf_id, chapter_id = _seed_parsed_pdf(
        tmp_path / "pdf_api.db", _user_id(tmp_path / "pdf_api.db")
    )
    resp = client.patch(
        f"/pdfs/{pdf_id}/chapters/{chapter_id}",
        json={"start_page": 3},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_page"] == 3 and body["end_page"] == 10
    assert body["name"] == "旧名"


def test_pdfs_api_patch_chapter_all_none_400(client: TestClient) -> None:
    """部分更新（fix round 1）：全 None → 400 VALIDATION_ERROR（先于状态裁决）。"""
    user = _user(client)
    file_id = client.post(
        "/pdfs",
        files={"file": ("e.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["file_id"]
    resp = client.patch(
        f"/pdfs/{file_id}/chapters/{uuid.uuid4()}",
        json={},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_pdfs_api_delete_chapter_204_and_removed(client: TestClient, tmp_path: Path) -> None:
    """删除章节：204 → 详情 chapters 不含该章；跨用户 404。"""
    user = _user(client)
    assert client.get("/pdfs", headers=user).status_code == 200  # 注册设备行（FK 前置）
    pdf_id, chapter_id = _seed_parsed_pdf(
        tmp_path / "pdf_api.db", _user_id(tmp_path / "pdf_api.db")
    )
    resp = client.delete(f"/pdfs/{pdf_id}/chapters/{chapter_id}", headers={**user, **_idem()})
    assert resp.status_code == 204, resp.text
    resp = client.get(f"/pdfs/{pdf_id}", headers=user)
    assert resp.status_code == 200
    assert all(c["chapter_id"] != chapter_id for c in resp.json()["chapters"])


def test_pdfs_api_delete_chapter_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    """跨设备删除章节 → 404（统一 404 不暴露存在性）。"""
    user = _user(client)
    assert client.get("/pdfs", headers=user).status_code == 200
    pdf_id, chapter_id = _seed_parsed_pdf(
        tmp_path / "pdf_api.db", _user_id(tmp_path / "pdf_api.db")
    )
    resp = client.delete(
        f"/pdfs/{pdf_id}/chapters/{chapter_id}",
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 404
