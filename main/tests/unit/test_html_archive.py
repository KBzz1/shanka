"""services.projects.html_archive 单元测试（V25-D-38：校验/结构抽取/分诊）。"""

import pytest

from app.config import Settings
from app.errors import AppError, ErrorCode
from services.projects.html_archive import parse_html_archive, validate_html_upload


def _settings(
    *,
    threshold: int = 24_000,
    max_chars: int = 300_000,
    max_bytes: int = 20 * 1024 * 1024,
) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        single_chapter_max_chars=threshold,
        html_max_total_chars=max_chars,
        html_max_size_bytes=max_bytes,
    )


def _html(body: str, head: str = "<title>t</title>") -> bytes:
    return f"<!DOCTYPE html><html><head>{head}</head><body>{body}</body></html>".encode()


# --- 校验 ---


def test_validate_rejects_bad_extension_mime_size() -> None:
    settings = _settings()
    validate_html_upload(
        filename="a.html", content_type="text/html", data=_html("<p>x</p>"), settings=settings
    )
    validate_html_upload(
        filename="a.htm",
        content_type="application/octet-stream",
        data=_html("<p>x</p>"),
        settings=settings,
    )
    with pytest.raises(AppError) as e:
        validate_html_upload(
            filename="a.txt", content_type="text/html", data=_html("<p>x</p>"), settings=settings
        )
    assert e.value.code is ErrorCode.HTML_UPLOAD_INVALID
    with pytest.raises(AppError):
        validate_html_upload(
            filename="a.html", content_type="text/plain", data=_html("<p>x</p>"), settings=settings
        )
    with pytest.raises(AppError):
        validate_html_upload(
            filename="a.html", content_type="text/html", data=b"plain text", settings=settings
        )
    with pytest.raises(AppError):
        validate_html_upload(
            filename="a.html",
            content_type="text/html",
            data=_html("<p>x</p>"),
            settings=_settings(max_bytes=10),
        )


# --- 结构抽取 ---


def test_parse_headings_become_chapters() -> None:
    """超阈值 + 标题 ≥2 → 标题章节（最浅级），script/style 忽略（阈值压低以聚焦结构层）。"""
    section = "<p>" + "内容段落。" * 3000 + "</p>"
    data = _html(
        f"<h2>一 开场</h2>{section}"
        f"<h2>二 进阶</h2>{section}"
        f"<h3>小节不算</h3><p>小节内容</p>"
        f"<script>var x=1;</script><style>p{{}}</style>"
    )
    chapters, total = parse_html_archive(data, settings=_settings(threshold=100))
    names = [c.name for c in chapters]
    assert names == ["一 开场", "二 进阶"]  # h3 更深层不建章；无标题前导正文→无开篇
    assert total > 24_000
    assert all("var x" not in p for c in chapters for p in c.paragraphs)
    assert all(len(c.paragraphs) > 0 for c in chapters)


def test_parse_leading_text_gets_intro_chapter() -> None:
    """首个标题前有正文 → 开篇章。"""
    filler = "<p>" + "字" * 4000 + "</p>"
    data = _html(f"{filler}<h1>第一章</h1>{filler}<h1>第二章</h1>{filler}")
    chapters, _total = parse_html_archive(data, settings=_settings(threshold=100))
    assert [c.name for c in chapters] == ["开篇", "第一章", "第二章"]


def test_parse_shallowest_level_wins() -> None:
    """页面只有 h2/h3 时 h2 为章（最浅出现的标题级）。"""
    filler = "<p>" + "字" * 4000 + "</p>"
    data = _html(f"<h2>A</h2>{filler}<h3>A1</h3><p>x</p><h2>B</h2>{filler}")
    chapters, _ = parse_html_archive(data, settings=_settings(threshold=100))
    assert [c.name for c in chapters] == ["A", "B"]


def test_parse_small_or_unstructured_is_single_chapter() -> None:
    """≤ 阈值 / 无标题 / 标题仅 1 个 → 恒单章（分诊规则 2/3，零模型）。"""
    small = _html("<h2>一</h2><p>内容</p><h2>二</h2><p>内容</p>")
    chapters, total = parse_html_archive(small, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文" and total <= 24_000

    big_no_heading = _html("<p>" + "字" * 30_000 + "</p>")
    chapters, _ = parse_html_archive(big_no_heading, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文"

    big_single_heading = _html(f"<p>{'字' * 20000}</p><h1>唯一标题</h1><p>{'字' * 20000}</p>")
    chapters, _ = parse_html_archive(big_single_heading, settings=_settings())
    assert len(chapters) == 1 and chapters[0].name == "全文"


def test_parse_rejects_bad_encoding_and_overlimit() -> None:
    with pytest.raises(AppError) as e:
        parse_html_archive("<html><body>中文</body></html>".encode("gbk"), settings=_settings())
    assert e.value.code is ErrorCode.HTML_EXTRACT_FAILED
    with pytest.raises(AppError) as e:
        parse_html_archive(_html("<p>" + "字" * 10 + "</p>"), settings=_settings(max_chars=5))
    assert e.value.code is ErrorCode.HTML_UPLOAD_INVALID
    with pytest.raises(AppError):
        parse_html_archive(_html(""), settings=_settings())
