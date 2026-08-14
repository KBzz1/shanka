"""services.pdf 页文本测试（spec §4.1）：extract_pages 提取 + text_chunks 持久化。

覆盖：chunk_id 确定性、持久化/读取 roundtrip、重解析清理重建（幂等）、
load_pages 升序闭区间、page_text_map、extract_pages 全空/零页/缺失文件失败分支。
命名规范：test_<模块>_<行为>。
"""

import hashlib
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import PdfFile, User
from services.pdf.parser import PageText, extract_pages
from services.pdf.text_chunks import chunk_id_for, load_pages, page_text_map, persist_text_chunks

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")
SAMPLE_TOTAL_PAGES = 318


def _write_text_pages(path: Path, texts: list[str]) -> None:
    """构造多页文本 PDF：每页 content stream 手写文本 + Type1 Helvetica 资源，无 outline。

    构造法与 test_pdf_parser.py 同款（pypdf 无 create_text API）；content stream
    字面量限 ASCII（PDF 字符串编码），故断言文本用英文。
    """
    w = PdfWriter()
    for text in texts:
        page = w.add_blank_page(width=200, height=200)
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 72 160 Td ({text}) Tj ET".encode("ascii"))
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
    with path.open("wb") as f:
        w.write(f)


def _seed_pdf(session: Session, file_id: str = "pdf-1") -> None:
    """父行先落库（PRAGMA foreign_keys=ON 强制，V1 教训同款）：text_chunks.file_id FK pdf_files。"""
    session.add(
        User(
            user_id="user-1",
            username="u-1",
            email="u-1@example.com",
            password_hash="x",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
    session.add(
        PdfFile(
            file_id=file_id,
            user_id="user-1",
            filename="book.pdf",
            storage_key="k",
            size_bytes=100,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()


def test_chunk_id_deterministic() -> None:
    a = chunk_id_for("f1", 3, "内容 abc")
    b = chunk_id_for("f1", 3, "内容 abc")
    assert a == b
    assert chunk_id_for("f1", 3, "改") != a


def test_persist_and_load_roundtrip(session: Session) -> None:
    _seed_pdf(session)
    pages: list[PageText] = [
        {"page_number": 1, "content": "第一页"},
        {"page_number": 2, "content": "第二页"},
    ]
    persist_text_chunks(session, file_id="pdf-1", pages=pages, now="2026-08-12T00:00:00.000Z")
    session.commit()
    rows = load_pages(session, file_id="pdf-1", start_page=1, end_page=2)
    assert [r.page_number for r in rows] == [1, 2]
    assert rows[0].content == "第一页"
    # chunk_id 落库 = 确定性生成值；char_count/content_sha256/created_at 按列记录
    assert rows[0].chunk_id == chunk_id_for("pdf-1", 1, "第一页")
    assert rows[0].char_count == len("第一页")
    assert rows[0].content_sha256 == hashlib.sha256("第一页".encode()).hexdigest()
    assert rows[0].created_at == "2026-08-12T00:00:00.000Z"


def test_reparse_rebuilds_and_cascades(session: Session) -> None:
    _seed_pdf(session)
    persist_text_chunks(
        session,
        file_id="pdf-1",
        pages=[{"page_number": 1, "content": "v1"}],
        now="2026-08-12T00:00:00.000Z",
    )
    persist_text_chunks(
        session,
        file_id="pdf-1",
        pages=[{"page_number": 1, "content": "v2"}],
        now="2026-08-12T00:00:00.000Z",
    )
    session.commit()
    assert len(load_pages(session, file_id="pdf-1", start_page=1, end_page=5)) == 1
    assert load_pages(session, file_id="pdf-1", start_page=1, end_page=5)[0].content == "v2"


def test_load_pages_ascending_inclusive_range(session: Session) -> None:
    _seed_pdf(session)
    pages: list[PageText] = [
        {"page_number": 3, "content": "third"},
        {"page_number": 1, "content": "first"},
        {"page_number": 2, "content": "second"},
    ]
    persist_text_chunks(session, file_id="pdf-1", pages=pages, now="2026-08-12T00:00:00.000Z")
    session.commit()
    # 闭区间 [start, end]，page_number 升序（与插入顺序无关）
    rows = load_pages(session, file_id="pdf-1", start_page=2, end_page=3)
    assert [r.page_number for r in rows] == [2, 3]
    assert [
        r.page_number for r in load_pages(session, file_id="pdf-1", start_page=1, end_page=5)
    ] == [1, 2, 3]


def test_page_text_map(session: Session) -> None:
    _seed_pdf(session)
    pages: list[PageText] = [
        {"page_number": 1, "content": "第一页"},
        {"page_number": 2, "content": "第二页"},
    ]
    persist_text_chunks(session, file_id="pdf-1", pages=pages, now="2026-08-12T00:00:00.000Z")
    session.commit()
    assert page_text_map(load_pages(session, file_id="pdf-1", start_page=1, end_page=2)) == {
        1: "第一页",
        2: "第二页",
    }


def test_extract_pages_returns_all_pages_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "two.pdf"
    _write_text_pages(path, ["first page", "second page"])
    pages = extract_pages(path)
    assert pages == [
        {"page_number": 1, "content": "first page"},
        {"page_number": 2, "content": "second page"},
    ]


def test_extract_pages_keeps_empty_page(tmp_path: Path) -> None:
    """部分页为空不失败：空页按 content="" 保留（spec §4.2 空页不影响其他页）。"""
    path = tmp_path / "mixed.pdf"
    w = PdfWriter()
    page = w.add_blank_page(width=200, height=200)
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 160 Td (first page) Tj ET")
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
    w.add_blank_page(width=200, height=200)  # 空页：无内容流
    with path.open("wb") as f:
        w.write(f)
    pages = extract_pages(path)
    assert [p["page_number"] for p in pages] == [1, 2]
    assert pages[1]["content"] == ""


def test_extract_pages_all_empty_raises(tmp_path: Path) -> None:
    path = tmp_path / "blank.pdf"
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with path.open("wb") as f:
        w.write(f)
    with pytest.raises(AppError) as excinfo:
        extract_pages(path)
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_extract_pages_zero_pages_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    w = PdfWriter()
    with path.open("wb") as f:
        w.write(f)
    with pytest.raises(AppError) as excinfo:
        extract_pages(path)
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_extract_pages_missing_file_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        extract_pages(Path("/nonexistent/x.pdf"))
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_extract_pages_sample_book_all_pages() -> None:
    """样书：318 页一页一行（与 services/pdf/AGENTS.md 校准常量一致）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    pages = extract_pages(SAMPLE)
    assert len(pages) == SAMPLE_TOTAL_PAGES
    assert [p["page_number"] for p in pages] == list(range(1, SAMPLE_TOTAL_PAGES + 1))
    assert sum(len(p["content"]) for p in pages) > 0
