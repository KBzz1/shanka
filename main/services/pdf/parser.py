"""services.pdf.parser：PDF 文本层检测 + 书签目录解析（pypdf）。

规则（structure-contract 5.1/5.2/AC-01，Task 1 报告 §章节规则）：仅文本层 + 目录；
文本层不可提取 → PDF_PARSE_FAILED；无可用目录 → PDF_TOC_MISSING；
不 OCR、不猜测、不兜底（无 OCR 兜底）。

章节规则（按样书实测校准，见 Task 1 报告）：
- 章节 = outline 顶层条目（根列表的直接子条目，depth 1）；
- 样书实测：318 页，outline 两层（顶层 = 引言/第 1-10 章/后记 共 12 条目，均有页码），
  故顶层条目即为章节；
- 防御性回退：若顶层条目全部无页码，取第一个含有效页码的深度层（"多层时取含页码层级"）；
- 页码归一化：get_destination_page_number 返回 0-based → +1 转 1-based；
  end_page = 下一条 start_page - 1，最后一条 = 总页数；越界 clamp（1 <= start <= end <= 总页数）。
"""

from io import BytesIO
from pathlib import Path
from typing import TypedDict, cast

from pypdf import PdfReader
from pypdf.generic import Destination

from app.errors import AppError, ErrorCode

_TEXT_SAMPLE_PAGES = 5
_TEXT_SAMPLE_LIMIT = 500


def page_count_hint(data: bytes) -> int | None:
    """上传页数 hint（carry-forward 决策）：pypdf 快速读页数；损坏文件 → None（扫描器 FAILED 兜底）。

    供 POST /projects、POST /projects/{id}/replace-pdf 与兼容 POST /pdfs 共用（单一来源）。
    """
    try:
        return len(PdfReader(BytesIO(data)).pages)
    except Exception:  # noqa: BLE001  # 损坏/非 PDF 一律无 hint，上传校验与扫描器兜底
        return None


class ChapterInfo(TypedDict):
    """章节区间（页码 1-based，闭区间）。"""

    name: str
    start_page: int
    end_page: int


class PageText(TypedDict):
    """单页完整文本（页码 1-based；空页 content="" 保留）。"""

    page_number: int
    content: str


def extract_text_ok(path: Path) -> bool:
    """文本层探测：抽样页（前 5 页）可提取非空文本。不抛异常，失败返回 False。"""
    try:
        reader = PdfReader(str(path))
        for page in reader.pages[:_TEXT_SAMPLE_PAGES]:
            text = page.extract_text() or ""
            if text.strip():
                return True
    except Exception:  # noqa: BLE001  # 探测必须兜底：pypdf 对损坏 PDF 抛多种异常，一律视为无文本层
        return False
    return False


def _outline_items(reader: PdfReader) -> list[tuple[str, int, int]]:
    """outline 展平为 (标题, 1-based 页码, 深度)。

    深度约定：根列表的直接子条目 = depth 1（brief 草案约定，见报告）。
    无页码条目（get_destination_page_number 抛错或返回 None）跳过，不产生章节。
    """

    items: list[tuple[str, int, int]] = []

    def walk(node: object, depth: int = 0) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child, depth + 1)
            return
        title = getattr(node, "title", "") or ""
        try:
            page = reader.get_destination_page_number(cast(Destination, node))
        except Exception:  # noqa: BLE001  # 损坏/异常 destination 一律视为无页码，不产生章节
            page = None
        if title.strip() and page is not None and page >= 0:
            items.append((title.strip(), page + 1, depth))

    if reader.outline is not None:
        walk(reader.outline)
    return items


def parse_pdf(path: Path) -> tuple[str, list[ChapterInfo]]:
    """解析 PDF：返回 (文本层样例, 章节列表)。

    文本层：抽样前 5 页拼接（超 500 字符截断）作为样例（AC-08：不落日志/不落库全文）。
    章节：outline 顶层条目（含页码的最浅深度层）；区间归一化与 clamp 见模块 docstring。
    """
    if not path.exists():
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 文件不存在")
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 无法打开") from exc

    # 文本层探测：前 5 页拼接样例（>500 字符截断）
    text_sample = ""
    try:
        for page in reader.pages[:_TEXT_SAMPLE_PAGES]:
            text_sample += page.extract_text() or ""
            if len(text_sample) > _TEXT_SAMPLE_LIMIT:
                break
    except Exception:  # noqa: BLE001  # 提取中途异常按无文本层处理（与 extract_text_ok 语义一致）
        text_sample = ""
    if not text_sample.strip():
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 无可提取文本层")

    # 目录解析：outline 展平 → 取含页码的最浅深度层作为章节
    items = _outline_items(reader)
    if not items:
        raise AppError(ErrorCode.PDF_TOC_MISSING, "PDF 无可识别目录结构")
    chapter_items = [it for it in items if it[2] == min(item[2] for item in items)]

    total_pages = len(reader.pages)
    chapters: list[ChapterInfo] = []
    for i, (name, start_page, _) in enumerate(chapter_items):
        start = min(start_page, total_pages)
        if i + 1 < len(chapter_items):
            end = chapter_items[i + 1][1] - 1
        else:
            end = total_pages
        end = max(start, min(end, total_pages))
        chapters.append(ChapterInfo(name=name, start_page=start, end_page=end))

    return text_sample[: _TEXT_SAMPLE_LIMIT + 1], chapters


def extract_pages(path: Path) -> list[PageText]:
    """提取完整页文本：一页一行（spec §4.1），page_number 1-based。

    规则：逐页 `extract_text() or ""`（空页 content="" 保留）；总页数 0 或全部
    页文本为空 → PDF_PARSE_FAILED；单页提取异常按空页处理（与 parse_pdf 抽样
    语义一致）。章节解析与文本样例走 parse_pdf，此处不做目录解析。
    """
    if not path.exists():
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 文件不存在")
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 无法打开") from exc

    pages: list[PageText] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            content = page.extract_text() or ""
        except Exception:  # noqa: BLE001  # 单页提取异常按空页处理（与 parse_pdf 抽样语义一致）
            content = ""
        pages.append(PageText(page_number=page_number, content=content))
    if not pages or not any(p["content"].strip() for p in pages):
        raise AppError(ErrorCode.PDF_PARSE_FAILED, "PDF 无可提取文本层")
    return pages
