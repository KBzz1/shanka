"""services.projects.markdown_doc 单元测试（V25-D-40：校验/结构抽取/分诊）。"""

import pytest

from app.config import Settings
from app.errors import AppError, ErrorCode
from services.projects.markdown_doc import parse_markdown_document, validate_markdown_upload


def _settings(
    *,
    threshold: int = 24_000,
    max_chars: int = 300_000,
    max_bytes: int = 20 * 1024 * 1024,
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        single_chapter_max_chars=threshold,
        markdown_max_total_chars=max_chars,
        markdown_max_size_bytes=max_bytes,
    )


# --- 校验 ---


def test_validate_rejects_bad_extension_mime_size() -> None:
    settings = _settings()
    validate_markdown_upload(
        filename="a.md", content_type="text/markdown", size_bytes=10, settings=settings
    )
    validate_markdown_upload(
        filename="a.markdown",
        content_type="application/octet-stream",
        size_bytes=10,
        settings=settings,
    )
    with pytest.raises(AppError) as e:
        validate_markdown_upload(
            filename="a.txt", content_type="text/markdown", size_bytes=10, settings=settings
        )
    assert e.value.code is ErrorCode.MARKDOWN_UPLOAD_INVALID
    with pytest.raises(AppError):
        validate_markdown_upload(
            filename="a.md", content_type="application/pdf", size_bytes=10, settings=settings
        )
    with pytest.raises(AppError):
        validate_markdown_upload(
            filename="a.md",
            content_type="text/markdown",
            size_bytes=20,
            settings=_settings(max_bytes=10),
        )


# --- 结构抽取 ---


def test_parse_headings_become_chapters() -> None:
    """超阈值 + ATX 标题 ≥2 → 标题章节（最浅级），围栏内 # 不计（阈值压低聚焦结构层）。"""
    section = "内容段落。" * 3000
    data = (
        f"## 一 开场\n\n{section}\n\n"
        f"## 二 进阶\n\n{section}\n\n"
        "### 小节不算\n\n小节内容\n\n"
        "```python\n# 这行不是标题\nvar = 1\n```\n"
    ).encode()
    chapters, total = parse_markdown_document(data, settings=_settings(threshold=100))
    names = [c.name for c in chapters]
    assert names == ["一 开场", "二 进阶"]  # ### 更深层不建章；无标题前导正文→无开篇
    assert total > 24_000
    assert all("# 这行不是标题" not in p or "```" in p for c in chapters for p in c.paragraphs)
    assert all(len(c.paragraphs) > 0 for c in chapters)


def test_parse_leading_text_gets_intro_chapter() -> None:
    """首个标题前有正文 → 开篇章；frontmatter 丢弃。"""
    filler = "字" * 4000
    data = f"---\ntitle: 笔记\n---\n\n{filler}\n\n# 第一章\n\n{filler}\n\n# 第二章\n\n{filler}\n".encode()
    chapters, _total = parse_markdown_document(data, settings=_settings(threshold=100))
    assert [c.name for c in chapters] == ["开篇", "第一章", "第二章"]
    assert all("title:" not in p for c in chapters for p in c.paragraphs)


def test_parse_shallowest_level_wins() -> None:
    """文档只有 ##/### 时 ## 为章（最浅出现的标题级）；列表保留块内换行。"""
    filler = "字" * 4000
    data = f"## A\n\n{filler}\n\n### A1\n\n- 项一\n- 项二\n\n## B\n\n{filler}\n".encode()
    chapters, _ = parse_markdown_document(data, settings=_settings(threshold=100))
    assert [c.name for c in chapters] == ["A", "B"]
    lists = [p for c in chapters for p in c.paragraphs if "项一" in p]
    assert lists and "\n- 项二" in lists[0]


def test_parse_small_or_unstructured_is_single_chapter() -> None:
    """≤ 阈值 / 无标题 / 标题仅 1 个 → 恒单章（分诊规则 2/3，零模型）。"""
    small = "## 一\n\n内容\n\n## 二\n\n内容\n".encode()
    chapters, total = parse_markdown_document(small, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文" and total <= 24_000

    big_no_heading = ("字" * 30_000 + "\n\n").encode()
    chapters, _ = parse_markdown_document(big_no_heading, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文"

    big_single_heading = f"{'字' * 20000}\n\n# 唯一标题\n\n{'字' * 20000}\n".encode()
    chapters, _ = parse_markdown_document(big_single_heading, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文"


def test_parse_rejects_bad_encoding_overlimit_and_empty() -> None:
    with pytest.raises(AppError) as e:
        parse_markdown_document("中文内容".encode("gbk"), settings=_settings())
    assert e.value.code is ErrorCode.MARKDOWN_EXTRACT_FAILED
    with pytest.raises(AppError) as e:
        parse_markdown_document("内容".encode(), settings=_settings(max_chars=1))
    assert e.value.code is ErrorCode.MARKDOWN_UPLOAD_INVALID
    with pytest.raises(AppError):
        parse_markdown_document(b"", settings=_settings())
    with pytest.raises(AppError):  # 仅 frontmatter 无正文
        parse_markdown_document(b"---\ntitle: x\n---\n", settings=_settings())
