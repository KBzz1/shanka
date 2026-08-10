"""services.pdf.parser 集成测试：样书解析 + 失败分支（真实 pypdf）。

命名规范：test_pdf_parser_<行为>。
构造样本说明（Task 1 报告 §TOC 样本方案）：
- pypdf 无 create_text API（6.15 实测 AttributeError），add_blank_page 也不产生文本层；
- "有文本层无 outline"样本用手写 content stream（BT/Tf/Td/Tj）+ Type1 Helvetica 字体资源构造，
  extract_text 可提取（逆验证见 test_pdf_parser_no_toc_raises 内 extract_text_ok 断言）。
"""

from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.errors import AppError, ErrorCode
from services.pdf.parser import extract_text_ok, parse_pdf

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")

# Step 1 样书实测校准值：318 页；outline 顶层 12 条目（引言/第 1-10 章/后记），
# 引言 0-based 页 8 → 1-based 9；末条目 后记 0-based 315 → 1-based 316。
SAMPLE_CHAPTERS = 12
SAMPLE_FIRST_START = 9
SAMPLE_TOTAL_PAGES = 318


def _write_text_page(path: Path, text: str = "hello world") -> None:
    """构造 1 页 PDF：content stream 手写文本 + Type1 Helvetica 资源，无 outline。"""
    w = PdfWriter()
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


def _write_blank_page(path: Path) -> None:
    """构造 1 页空白 PDF：无内容流、无 outline（extract_text 为空）。"""
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    with path.open("wb") as f:
        w.write(f)


def test_pdf_parser_sample_book_parses_chapters() -> None:
    """样书：文本层可用 + 目录解析出章节（outline 顶层条目作为章节）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    text, chapters = parse_pdf(SAMPLE)
    assert text  # 文本层非空
    # 校准断言（Step 1 实测）：顶层 12 条目、首章 1-based 起始页 9、末章 end_page = 总页数
    assert len(chapters) == SAMPLE_CHAPTERS
    assert chapters[0]["start_page"] == SAMPLE_FIRST_START
    assert chapters[-1]["end_page"] == SAMPLE_TOTAL_PAGES
    starts: list[int] = []
    for ch in chapters:
        assert ch["name"]
        assert ch["start_page"] >= 1
        assert ch["end_page"] >= ch["start_page"]
        assert ch["end_page"] <= SAMPLE_TOTAL_PAGES
        starts.append(ch["start_page"])
    # 章节按 outline 顺序、起始页严格递增（归一化后不越界）
    assert starts == sorted(starts)


def test_pdf_parser_missing_file_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        parse_pdf(Path("/nonexistent/x.pdf"))
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_pdf_parser_no_text_layer_raises(tmp_path: Path) -> None:
    """无文本层（空白页 extract_text 为空）→ PDF_PARSE_FAILED。"""
    path = tmp_path / "blank.pdf"
    _write_blank_page(path)
    with pytest.raises(AppError) as excinfo:
        parse_pdf(path)
    assert excinfo.value.code is ErrorCode.PDF_PARSE_FAILED


def test_pdf_parser_no_toc_raises(tmp_path: Path) -> None:
    """有文本层但无目录 → PDF_TOC_MISSING（构造：content stream 文本页 + 无 outline）。"""
    path = tmp_path / "notoc.pdf"
    _write_text_page(path)
    assert extract_text_ok(path)  # 逆验证：构造样本确有可提取文本层
    with pytest.raises(AppError) as excinfo:
        parse_pdf(path)
    assert excinfo.value.code is ErrorCode.PDF_TOC_MISSING


def test_pdf_parser_extract_text_ok_probes(tmp_path: Path) -> None:
    """extract_text_ok：样书 True；空白/缺失文件 False。"""
    if SAMPLE.exists():
        assert extract_text_ok(SAMPLE)
    blank = tmp_path / "blank.pdf"
    _write_blank_page(blank)
    assert extract_text_ok(blank) is False
    assert extract_text_ok(Path("/nonexistent/x.pdf")) is False
