"""services.projects.html_archive：HTML 页面资料的同步解析（V25-D-38）。

程序优先（零模型、零新依赖——标准库 ``html.parser``）：

- 校验：``.html``/``.htm`` 扩展 + 宽松 html 魔数 + ``text/html``（或 octet-stream 兜底）
  MIME + ≤ ``html_max_size_bytes``；正文总字符 ≤ ``html_max_total_chars``；
- 解析：按文档顺序遍历，``<h1>``~``<h6>`` 为结构标记——**最浅出现的标题级**为章节级
  （页面只有 h2/h3 时以 h2 为章），标题间正文按空行段落切段（TEXT/ZIP 同款）；
  ``script``/``style``/``noscript``/``template`` 及注释忽略；
- 章节产出：标题级出现 ≥2 次 → 每个标题一章节（source=HEADING，chunk_seq 连续区间）；
  无标题、标题级只出现 1 次、或总字符 ≤ ``single_chapter_max_chars`` → 恒单章
  （source=AUTO，分诊规则 2/3——同步类型不走 AI）；
- 非 UTF-8 解码失败 → ``HTML_EXTRACT_FAILED``；抽取不到任何正文 → ``HTML_UPLOAD_INVALID``。
"""

import re
from html import unescape
from html.parser import HTMLParser
from typing import NamedTuple

from app.config import Settings
from app.errors import AppError, ErrorCode

_IGNORED_TAGS = {"script", "style", "noscript", "template", "head", "svg"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# 宽松魔数：文档前 2KB 内出现任一即视为 html（允许 BOM/前导空白与 doctype 变体）
_HTML_MAGIC = re.compile(r"<(!doctype\s+html|html|head|body|div|p|meta|title)[\s>/]", re.IGNORECASE)


class HtmlChapter(NamedTuple):
    """一个章节：标题 + 该章的段落文本列表（切段由调用方执行）。"""

    name: str
    paragraphs: list[str]


class _StructureExtractor(HTMLParser):
    """单遍抽取：正文段落流 + (标题文本, 段落下标) 边界标记。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        # (最浅标题级序号 1..6, 标题文本, 边界后首段下标)
        self.marks: list[tuple[int, str, int]] = []
        self._ignored_depth = 0
        self._capture: list[str] | None = None
        self._capture_tag: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored_depth:
            if tag in _IGNORED_TAGS:
                self._ignored_depth += 1
            return
        if tag in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if tag in _HEADING_TAGS:
            self._flush_paragraph()
            self._capture = []
            self._capture_tag = tag
        elif tag in ("p", "br", "li", "tr") or tag in _HEADING_TAGS:
            self._flush_paragraph()

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_depth:
            if tag in _IGNORED_TAGS:
                self._ignored_depth -= 1
            return
        if tag in _HEADING_TAGS and self._capture is not None:
            text = unescape("".join(self._capture)).strip()
            self._capture = None
            self._capture_tag = None
            if text:
                level = int(tag[1])
                self.marks.append((level, text, len(self.paragraphs)))
        elif tag in ("p", "li", "tr"):
            self._flush_paragraph()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._capture is not None:
            self._capture.append(data)
        else:
            self._buffer.append(data)

    def _flush_paragraph(self) -> None:
        text = unescape("".join(self._buffer)).strip()
        self._buffer = []
        # 块级标签内的行内换行折叠为空格（html 展示语义）；段落内空白归一
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            self.paragraphs.append(text)


def validate_html_upload(
    *,
    filename: str,
    content_type: str,
    data: bytes,
    settings: Settings,
) -> None:
    """上传三重校验 + 限制（6.2）：扩展名/魔数/MIME + ≤20MB。"""
    ok_ext = filename.lower().endswith((".html", ".htm"))
    ok_magic = bool(_HTML_MAGIC.search(data[:2048].decode("ascii", errors="ignore")))
    ok_mime = content_type.lower() in (
        "text/html",
        "application/xhtml+xml",
        "application/octet-stream",
    )
    ok_size = len(data) <= settings.html_max_size_bytes
    if not (ok_ext and ok_magic and ok_mime and ok_size):
        reasons = []
        if not ok_ext:
            reasons.append(f"扩展名非 .html/.htm（{filename!r}）")
        if not ok_magic:
            reasons.append("文件头非 HTML 文档")
        if not ok_mime:
            reasons.append(f"MIME 非 text/html（{content_type}）")
        if not ok_size:
            reasons.append(
                f"超过 {settings.html_max_size_bytes // (1024 * 1024)}MB 限制（{len(data)} bytes）"
            )
        raise AppError(ErrorCode.HTML_UPLOAD_INVALID, "HTML 文件校验失败：" + "；".join(reasons))


def parse_html_archive(data: bytes, *, settings: Settings) -> tuple[list[HtmlChapter], int]:
    """HTML 字节 → (章节列表, 正文总字符)。

    分诊（V25-D-38，同步类型零模型）：总字符 ≤ single_chapter_max_chars、或无有效
    标题结构（标题级成员 <2）→ 单章；否则最浅出现的标题级 = 章节级，标题前正文
    收「开篇」章。总字符 > html_max_total_chars → HTML_UPLOAD_INVALID。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AppError(ErrorCode.HTML_EXTRACT_FAILED, "HTML 非 UTF-8 编码") from exc
    extractor = _StructureExtractor()
    try:
        extractor.feed(text)
        extractor.close()
    except Exception as exc:
        raise AppError(ErrorCode.HTML_EXTRACT_FAILED, "HTML 解析失败") from exc
    extractor._flush_paragraph()
    paragraphs = extractor.paragraphs
    total_chars = sum(len(p) for p in paragraphs)
    if total_chars > settings.html_max_total_chars:
        raise AppError(
            ErrorCode.HTML_UPLOAD_INVALID,
            f"正文超过 {settings.html_max_total_chars} 字符限制（{total_chars}）",
        )
    if not paragraphs:
        raise AppError(ErrorCode.HTML_UPLOAD_INVALID, "HTML 无可提取正文")

    if total_chars <= settings.single_chapter_max_chars:
        return [HtmlChapter(name="全文", paragraphs=paragraphs)], total_chars

    # 章节级 = 最浅出现的标题级；成员 ≥2 才构成结构，否则单章（分诊规则 3）
    if extractor.marks:
        top_level = min(level for level, _, _ in extractor.marks)
        level_marks = [(name, idx) for level, name, idx in extractor.marks if level == top_level]
    else:
        level_marks = []
    if len(level_marks) < 2:
        return [HtmlChapter(name="全文", paragraphs=paragraphs)], total_chars

    chapters: list[HtmlChapter] = []
    if level_marks[0][1] > 0:
        chapters.append(HtmlChapter(name="开篇", paragraphs=paragraphs[: level_marks[0][1]]))
    for index, (name, start) in enumerate(level_marks):
        end = level_marks[index + 1][1] if index + 1 < len(level_marks) else len(paragraphs)
        chapters.append(HtmlChapter(name=name, paragraphs=paragraphs[start:end]))
    return chapters, total_chars
