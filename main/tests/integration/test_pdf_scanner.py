"""services.pdf.scanner 集成测试：状态机/章节落库/失败分支/恢复。

V1 教训 carry-forward：devices FK 强制（PRAGMA foreign_keys=ON），scanner 测试
需显式建立 devices 行（见 test_pdf_service.py 同款 _ensure_device）。
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import Base, Chapter, Device, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from infra.storage.local import LocalStorage
from services.pdf.scanner import process_pending, scan_once, validate_upload

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_device(session: Session, device_id: str) -> None:
    """devices 行先落库（FK 强制）：scanner 测试需显式建立。"""
    session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
    session.flush()


def _seed_pending(session: Session, *, device_id: str, storage_key: str) -> str:
    _ensure_device(session, device_id)
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="book.pdf",
        storage_key=storage_key,
        size_bytes=100,
        status="PENDING",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    return pdf.file_id


def test_scanner_process_pending_parses_sample(
    session_factory: sessionmaker[Session], storage: LocalStorage
) -> None:
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _uuid()
    with session_factory() as session:
        storage_key = storage.save(SAMPLE.read_bytes())
        file_id = _seed_pending(session, device_id=device, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage)
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
    assert n == 1
    assert row is not None
    assert row.status == "PARSED"
    assert len(chapters) >= 3
    assert chapters[0].start_page >= 1


def test_scanner_process_pending_failed_keeps_file(
    session_factory: sessionmaker[Session], storage: LocalStorage
) -> None:
    """损坏 PDF → FAILED + error_code，原始文件保留。"""
    device = _uuid()
    with session_factory() as session:
        storage_key = storage.save(b"not a real pdf content")
        file_id = _seed_pending(session, device_id=device, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage)
        session.commit()
        row = session.get(PdfFile, file_id)
    assert n == 1
    assert row is not None
    assert row.status == "FAILED"
    assert row.error_code == "PDF_PARSE_FAILED"
    assert storage.open(row.storage_key).exists()  # 原始文件保留（5.1）


def test_scanner_scan_once_resumes_after_restart(
    session_factory: sessionmaker[Session], storage: LocalStorage
) -> None:
    """重启恢复：PENDING/PARSING 残留重新入队处理。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _uuid()
    with session_factory() as session:
        key1 = storage.save(SAMPLE.read_bytes())
        f1 = _seed_pending(session, device_id=device, storage_key=key1)
        # PARSING 残留（模拟崩溃）
        key2 = storage.save(SAMPLE.read_bytes())
        pdf2 = PdfFile(
            file_id=_uuid(),
            device_id=device,
            filename="b2.pdf",
            storage_key=key2,
            size_bytes=100,
            status="PARSING",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf2)
        session.flush()
        f2 = pdf2.file_id
        session.commit()
    # 新 session/新 app（重启模拟）
    with session_factory() as session:
        n = scan_once(session_factory, storage=storage)
        assert n >= 2
    with session_factory() as session:
        row1 = session.get(PdfFile, f1)
        row2 = session.get(PdfFile, f2)
    assert row1 is not None and row2 is not None
    assert row1.status == "PARSED"
    assert row2.status == "PARSED"


def test_scanner_validate_upload_triple_check(tmp_path: Path) -> None:
    settings = Settings()
    # 合法
    validate_upload(
        filename="a.pdf",
        content_type="application/pdf",
        magic=b"%PDF-1.4",
        size_bytes=100,
        page_count_hint=None,
        settings=settings,
    )
    # 扩展名
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.txt",
            content_type="application/pdf",
            magic=b"%PDF-1.4",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # 魔数
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="application/pdf",
            magic=b"not-pdf",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # MIME
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="text/plain",
            magic=b"%PDF-1.4",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # 大小
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="application/pdf",
            magic=b"%PDF-1.4",
            size_bytes=51 * 1024 * 1024,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
