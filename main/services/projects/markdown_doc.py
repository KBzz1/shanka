"""services.projects.markdown_doc：Markdown 单文件资料的同步解析（V25-D-40）。

程序优先（零模型、零新依赖），分诊与 HTML 同款（V25-D-38）：

- 校验：``.md``/``.markdown`` 扩展 + ``text/markdown``（或 text/plain/octet-stream 兜底）
  MIME + ≤ ``markdown_max_size_bytes``；正文总字符 ≤ ``markdown_max_total_chars``；
- 解析：ATX 标题 ``#``~``######``（行首、围栏代码块内不计）为结构标记——**最浅出现的
  标题级**为章节级（文档只有 ##/### 时以 ## 为章），标题间正文按空行段落切段；
  YAML frontmatter（起始 ``---`` 至闭合 ``---``/``...``）与围栏代码块内容保留为正文，
  但不参与标题识别；
- 章节产出：标题级出现 ≥2 次 → 每个标题一章节（source=HEADING，chunk_seq 连续区间）；
  无标题、标题级只出现 1 次、或总字符 ≤ ``single_chapter_max_chars`` → 恒单章
  （source=AUTO，分诊规则 2/3——同步类型不走 AI）；
- 非 UTF-8 解码失败 → ``MARKDOWN_EXTRACT_FAILED``；抽取不到任何正文 →
  ``MARKDOWN_UPLOAD_INVALID``。
"""

import re
from typing import NamedTuple

from app.config import Settings
from app.errors import AppError, ErrorCode

# MIME 白名单：markdown 规范类型 + 常见 text/plain 兜底 + Android SAF octet-stream
_MD_MIME = frozenset({"text/markdown", "text/x-markdown", "text/plain", "application/octet-stream"})
# ATX 标题：行首 1~6 个 # + 空白 + 标题文本（尾随 # 修剪）
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# 围栏代码块开/闭：行首 ≥3 个反引号或波浪线
_FENCE = re.compile(r"^(`{3,}|~{3,})")


class MarkdownChapter(NamedTuple):
    """一个章节：标题 + 该章的段落文本列表（切段由调用方执行）。"""

    name: str
    paragraphs: list[str]


def validate_markdown_upload(
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
    settings: Settings,
) -> None:
    """上传校验（6.2）：扩展名/MIME + ≤ ``markdown_max_size_bytes``。

    md 无可靠魔数（纯文本），以扩展名 + MIME 双重信号为准（HTML/ZIP 三重校验的
    文本型退化形态）。
    """
    ok_ext = filename.lower().endswith((".md", ".markdown"))
    ok_mime = content_type.lower() in _MD_MIME
    ok_size = size_bytes <= settings.markdown_max_size_bytes
    if not (ok_ext and ok_mime and ok_size):
        reasons = []
        if not ok_ext:
            reasons.append(f"扩展名非 .md/.markdown（{filename!r}）")
        if not ok_mime:
            reasons.append(f"MIME 非 text/markdown（{content_type}）")
        if not ok_size:
            reasons.append(
                f"超过 {settings.markdown_max_size_bytes // (1024 * 1024)}MB 限制（{size_bytes} bytes）"
            )
        raise AppError(
            ErrorCode.MARKDOWN_UPLOAD_INVALID, "Markdown 文件校验失败：" + "；".join(reasons)
        )


def _strip_frontmatter(text: str) -> str:
    """丢弃 YAML frontmatter（首行 ``---`` 至首个闭合 ``---``/``...`` 行）。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            return "\n".join(lines[index + 1 :])
    return text  # 未闭合 → 按普通正文处理


def _split_paragraphs(text: str) -> tuple[list[str], list[tuple[int, str, int]]]:
    """正文 → (段落列表, (标题级 1..6, 标题文本, 边界后首段下标) 边界标记)。

    段落 = 空行分隔的连续行块（块内换行保留——列表/缩进结构原样进 chunk）；
    标题行自成边界（同 HTML 块级标签语义）；围栏代码块内的 ``#`` 不计标题。
    """
    paragraphs: list[str] = []
    marks: list[tuple[int, str, int]] = []
    buffer: list[str] = []
    fence: str | None = None

    def _flush() -> None:
        block = "\n".join(buffer).strip()
        buffer.clear()
        if block:
            paragraphs.append(block)

    for line in text.split("\n"):
        if fence is not None:
            buffer.append(line)
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = None
            continue
        opened = _FENCE.match(line)
        if opened:
            _flush()
            fence = opened.group(1)[:3]  # 闭合判定基线：≥3 个同字符
            buffer.append(line)
            continue
        heading = _ATX_HEADING.match(line)
        if heading:
            _flush()
            marks.append((len(heading.group(1)), heading.group(2).strip(), len(paragraphs)))
            continue
        if not line.strip():
            _flush()
            continue
        buffer.append(line)
    _flush()
    return paragraphs, marks


def parse_markdown_document(
    data: bytes, *, settings: Settings
) -> tuple[list[MarkdownChapter], int]:
    """Markdown 字节 → (章节列表, 正文总字符)。

    分诊（V25-D-38 同款，同步类型零模型）：总字符 ≤ single_chapter_max_chars、或无有效
    标题结构（标题级成员 <2）→ 单章；否则最浅出现的标题级 = 章节级，标题前正文
    收「开篇」章。总字符 > markdown_max_total_chars → MARKDOWN_UPLOAD_INVALID。
    """
    try:
        text = data.decode("utf-8").lstrip("\ufeff")
    except UnicodeDecodeError as exc:
        raise AppError(ErrorCode.MARKDOWN_EXTRACT_FAILED, "Markdown 文件非 UTF-8 编码") from exc
    paragraphs, marks = _split_paragraphs(_strip_frontmatter(text))
    total_chars = sum(len(p) for p in paragraphs)
    if total_chars > settings.markdown_max_total_chars:
        raise AppError(
            ErrorCode.MARKDOWN_UPLOAD_INVALID,
            f"正文超过 {settings.markdown_max_total_chars} 字符限制（{total_chars}）",
        )
    if not paragraphs:
        raise AppError(ErrorCode.MARKDOWN_UPLOAD_INVALID, "Markdown 无可提取正文")

    if total_chars <= settings.single_chapter_max_chars:
        return [MarkdownChapter(name="全文", paragraphs=paragraphs)], total_chars

    # 章节级 = 最浅出现的标题级；成员 ≥2 才构成结构，否则单章（分诊规则 3）
    if marks:
        top_level = min(level for level, _, _ in marks)
        level_marks = [(name, idx) for level, name, idx in marks if level == top_level]
    else:
        level_marks = []
    if len(level_marks) < 2:
        return [MarkdownChapter(name="全文", paragraphs=paragraphs)], total_chars

    chapters: list[MarkdownChapter] = []
    if level_marks[0][1] > 0:
        chapters.append(MarkdownChapter(name="开篇", paragraphs=paragraphs[: level_marks[0][1]]))
    for index, (name, start) in enumerate(level_marks):
        end = level_marks[index + 1][1] if index + 1 < len(level_marks) else len(paragraphs)
        chapters.append(MarkdownChapter(name=name, paragraphs=paragraphs[start:end]))
    return chapters, total_chars
