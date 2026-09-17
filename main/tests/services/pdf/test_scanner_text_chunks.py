"""services.pdf.scanner 页文本接线测试（spec §4.1，扩展既有 test_pdf_scanner.py）。

新增断言：PARSED 后完整页文本落 text_chunks（一页一行、chunk_id 确定性、
重解析清理重建、PDF 删除按 file_id 级联清理）；解析失败不写页文本。
命名规范：test_<模块>_<行为>。
"""

import uuid
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from infra.db.models import LearningProject, Material, PdfFile, TextChunk, User
from infra.storage.local import LocalStorage
from services.pdf.scanner import process_pending
from services.pdf.text_chunks import chunk_id_for

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")
SAMPLE_TOTAL_PAGES = 318


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_pending(session: Session, *, user_id: str, storage_key: str) -> str:
    """V25-D-29 基座：PDF 行必须伴随 LearningProject + Material（material_id == file_id），
    scanner 重建 chapters 时按 material_id FK 写回。"""
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="扫描项目",
        version="2026-08-11T00:00:00.000Z",
        created_at="2026-08-11T00:00:00.000Z",
        updated_at="2026-08-11T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="book.pdf",
        storage_key=storage_key,
        size_bytes=100,
        status="PENDING",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    session.add(
        Material(
            material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project.project_id,
            type="PDF",
            name="book.pdf",
            status=None,
            size_bytes=100,
            created_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()
    return pdf.file_id


def _chunk_rows(session: Session, file_id: str) -> list[TextChunk]:
    return list(
        session.scalars(
            select(TextChunk).where(TextChunk.file_id == file_id).order_by(TextChunk.page_number)
        ).all()
    )


def test_scanner_parsed_persists_text_chunks(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    """PARSED 后完整页文本落库：一页一行、1-based 页码、确定性 chunk_id。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(SAMPLE.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=settings)
        session.commit()
        rows = _chunk_rows(session, file_id)
    assert n == 1
    assert len(rows) == SAMPLE_TOTAL_PAGES
    assert [r.page_number for r in rows] == list(range(1, SAMPLE_TOTAL_PAGES + 1))
    assert sum(r.char_count for r in rows) > 0
    assert all(r.created_at for r in rows)
    # chunk_id = 确定性生成值（同一 (file_id, page, content) 页标识稳定）
    first = rows[0]
    assert first.chunk_id == chunk_id_for(file_id, first.page_number, first.content)


def test_scanner_reparse_rebuilds_text_chunks(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    """重解析幂等：清理重建后仍一页一行，且同内容页标识稳定（spec §4.1）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(SAMPLE.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        assert process_pending(session, storage=storage, settings=settings) == 1
        session.commit()
    # 模拟重解析：置回 PENDING 再走一遍
    with session_factory() as session:
        row = session.get(PdfFile, file_id)
        assert row is not None
        row.status = "PENDING"
        session.commit()
    with session_factory() as session:
        assert process_pending(session, storage=storage, settings=settings) == 1
        session.commit()
        rows = _chunk_rows(session, file_id)
    assert len(rows) == SAMPLE_TOTAL_PAGES
    for r in rows:
        assert r.chunk_id == chunk_id_for(file_id, r.page_number, r.content)


def test_scanner_delete_pdf_cascades_text_chunks(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    """删除 PDF 按 file_id 级联清理页文本（spec §4.1，FK ON DELETE CASCADE）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(SAMPLE.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        assert process_pending(session, storage=storage, settings=settings) == 1
        session.commit()
    with session_factory() as session:
        pdf = session.get(PdfFile, file_id)
        assert pdf is not None
        session.delete(pdf)
        session.commit()
    with session_factory() as session:
        assert _chunk_rows(session, file_id) == []


def test_scanner_no_toc_fails_without_text_chunks(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36：无目录 + 未存 Key → FAILED + API_KEY_NOT_SET；页文本先行落库
    （reparse / whole-book 降级不需重解析文件）。"""
    pdf_path = tmp_path / "notoc.pdf"
    w = PdfWriter()
    page = w.add_blank_page(width=200, height=200)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 160 Td (hello world) Tj ET")
    page[NameObject("/Contents")] = w._add_object(content)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = w._add_object(font)
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    page[NameObject("/Resources")] = w._add_object(resources)
    with pdf_path.open("wb") as f:
        w.write(f)
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=settings)
        session.commit()
        row = session.get(PdfFile, file_id)
        chunks = _chunk_rows(session, file_id)
    assert n == 1
    assert row is not None
    assert row.status == "PARSED"  # V25-D-38：小资料直接 AUTO 单章，未存 Key 也成功
    assert len(chunks) == 1  # 页文本落库
