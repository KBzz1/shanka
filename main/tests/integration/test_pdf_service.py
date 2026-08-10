"""services.pdf.service 集成测试：上传/列表/详情/删除/章节（真实 SQLite + 临时存储）。

V1 教训 carry-forward：devices FK 强制（PRAGMA foreign_keys=ON），service 层测试需
显式建立 devices 行（HTTP 流由设备中间件自动建立，见 test_decks_service.py）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Chapter, Device, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.storage.local import LocalStorage
from services.pdf.service import (
    delete_pdf,
    get_pdf,
    list_pdfs,
    update_chapter,
    upload_pdf,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pdf.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_device(session: Session, device_id: str) -> None:
    """devices 行先落库（FK 强制）：service 测试需显式建立。"""
    session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
    session.flush()


def _seed_pdf(
    session: Session, *, device_id: str, storage_key: str = "", status: str = "PARSED"
) -> str:
    _ensure_device(session, device_id)
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="book.pdf",
        storage_key=storage_key or _uuid(),
        size_bytes=100,
        status=status,
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    return pdf.file_id


def test_pdf_service_upload_creates_pending(
    session_factory: Callable[[], Session], storage: LocalStorage
) -> None:
    device = _uuid()
    with session_factory() as session:
        _ensure_device(session, device)
        pdf = upload_pdf(
            session,
            device_id=device,
            filename="book.pdf",
            size_bytes=100,
            storage_key=_uuid(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        file_id = pdf.file_id
    assert pdf.status == "PENDING"
    with session_factory() as session:
        row = session.get(PdfFile, file_id)
        assert row is not None
        assert row.device_id == device


def test_pdf_service_list_isolated_and_sorted(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        _seed_pdf(session, device_id=device_a)
        _seed_pdf(session, device_id=device_b)
        session.commit()
    with session_factory() as session:
        list_a = list_pdfs(session, device_id=device_a)
        list_b = list_pdfs(session, device_id=device_b)
    assert len(list_a) == 1 and len(list_b) == 1


def test_pdf_service_get_cross_device_404(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device_a)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_pdf(session, device_id=device_b, file_id=file_id)
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_pdf_service_delete_removes_and_cleans_storage(
    session_factory: Callable[[], Session], storage: LocalStorage, tmp_path: Path
) -> None:
    device = _uuid()
    storage_key = uuid.uuid4().hex  # storage.open 严格校验 32 位 hex（草稿瑕疵修正）
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device, storage_key=storage_key)
        session.commit()
    # 写存储对象（模拟扫描器/上传产物；storage.open 前需先建父目录）
    obj_path = storage.open(storage_key)
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(b"%PDF-1.4 fake")
    assert obj_path.exists()
    with session_factory() as session:
        delete_pdf(session, device_id=device, file_id=file_id, storage=storage)
        session.commit()
    with session_factory() as session:
        assert session.get(PdfFile, file_id) is None
    assert not obj_path.exists()  # 存储清理


def test_pdf_service_delete_blocked_by_non_terminal_task(
    session_factory: Callable[[], Session], storage: LocalStorage
) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device)
        session.add(
            Task(
                task_id=_uuid(),
                device_id=device,
                file_id=file_id,
                status="RUNNING",
                selected_chapters="[]",
                generation_config="{}",
                generated_card_count=0,
                resumable=0,
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        delete_pdf(session, device_id=device, file_id=file_id, storage=storage)
    assert excinfo.value.code is ErrorCode.TASK_IN_PROGRESS


def test_pdf_service_update_chapter(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device)
        ch = Chapter(chapter_id=_uuid(), file_id=file_id, name="旧名", start_page=1, end_page=10)
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session:
        updated = update_chapter(
            session,
            device_id=device,
            file_id=file_id,
            chapter_id=chapter_id,
            name="新名",
            start_page=2,
            end_page=8,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert updated.name == "新名"
    assert updated.start_page == 2 and updated.end_page == 8


def test_pdf_service_update_chapter_invalid_range(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device)
        ch = Chapter(chapter_id=_uuid(), file_id=file_id, name="c", start_page=1, end_page=10)
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        update_chapter(
            session,
            device_id=device,
            file_id=file_id,
            chapter_id=chapter_id,
            name="x",
            start_page=5,
            end_page=3,
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_pdf_service_update_chapter_not_parsed(session_factory: Callable[[], Session]) -> None:
    """非 PARSED 时 PATCH 章节 → 409 TASK_STATE_CONFLICT（裁决）。"""
    device = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, device_id=device, status="FAILED")
        ch = Chapter(chapter_id=_uuid(), file_id=file_id, name="c", start_page=1, end_page=5)
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        update_chapter(
            session,
            device_id=device,
            file_id=file_id,
            chapter_id=chapter_id,
            name="x",
            start_page=1,
            end_page=5,
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
