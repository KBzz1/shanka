"""ZIP 笔记包解析单测（V25-D-35）：结构规则矩阵 + 容器校验 + 编码恢复 + 超限防线。

契约锚点：structure-contract 3.2a/6.2 ZIP 限制与错误码；services/projects/zip_archive.py。
"""

import io
import zipfile
from typing import Any

import pytest

from app.config import Settings
from app.errors import AppError
from services.projects.zip_archive import parse_zip_archive, validate_zip_upload


def _settings(**overrides: int) -> Settings:
    return Settings(
        zip_max_size_bytes=overrides.get("zip_max_size_bytes", 20 * 1024 * 1024),
        zip_max_files=overrides.get("zip_max_files", 500),
        zip_max_total_chars=overrides.get("zip_max_total_chars", 300_000),
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    """内存构建 zip（UTF-8 flag 文件名）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _error_code(exc: AppError) -> str:
    return exc.code.value


def test_parse_happy_path_overview_first_and_natural_order() -> None:
    """正常结构：根级 md → 「总览」排最前；子文件夹章节按自然序（1. < 2. < 10.）。"""
    data = _zip_bytes(
        {
            "Notes/0.总览.md": "# 总览\n\n索引页。",
            "Notes/10.最后板块/b.md": "第十章内容。",
            "Notes/10.最后板块/a.md": "第十章第一篇。",
            "Notes/2.基础/b.md": "第二章内容。",
            "Notes/1.开始/a.md": "第一章内容。",
        }
    )
    archive = parse_zip_archive(data, settings=_settings())
    assert [c.name for c in archive.chapters] == ["总览", "1.开始", "2.基础", "10.最后板块"]
    assert archive.chapters[1].files[0][0].endswith("a.md")  # 章节内文件同样自然排序
    assert archive.total_chars == sum(
        len(content) for chapter in archive.chapters for _, content in chapter.files
    )


def test_parse_recursive_nested_md_collected_into_chapter() -> None:
    """V25-D-35：章节文件夹内嵌套子文件夹递归收集，按相对路径自然排序归入该章节。"""
    data = _zip_bytes(
        {
            "V/1.章/1.1/sub/deep.md": "深层笔记。",
            "V/1.章/1.1/top.md": "浅层笔记。",
        }
    )
    archive = parse_zip_archive(data, settings=_settings())
    assert [c.name for c in archive.chapters] == ["1.章"]
    assert [rel for rel, _ in archive.chapters[0].files] == [
        "1.章/1.1/sub/deep.md",
        "1.章/1.1/top.md",
    ]


def test_parse_ignores_non_md_and_junk_entries() -> None:
    """非 md 静默忽略；系统/编辑器垃圾（含 .obsidian、._*.md）不参与结构判定。"""
    data = _zip_bytes(
        {
            "V/1.章/a.md": "正文。",
            "V/1.章/screenshot.png": "binary-ish",
            "V/1.章/.DS_Store": "junk",
            "V/1.章/._a.md": "appledouble junk",
            ".obsidian/app.json": "editor config",
            "__MACOSX/V/1.章/._a.md": "macOS metadata",
        }
    )
    archive = parse_zip_archive(data, settings=_settings())
    assert [c.name for c in archive.chapters] == ["1.章"]
    assert [rel for rel, _ in archive.chapters[0].files] == ["1.章/a.md"]


def test_parse_multiple_top_level_entries_rejected() -> None:
    data = _zip_bytes({"A/a.md": "A 内容。", "B/b.md": "B 内容。"})
    with pytest.raises(AppError) as exc:
        parse_zip_archive(data, settings=_settings())
    assert _error_code(exc.value) == "ZIP_STRUCTURE_INVALID"


def test_parse_loose_root_files_rejected() -> None:
    """散落根级文件（无主文件夹）拒绝——即使只有一个。"""
    data = _zip_bytes({"readme.md": "无主文件夹。"})
    with pytest.raises(AppError) as exc:
        parse_zip_archive(data, settings=_settings())
    assert _error_code(exc.value) == "ZIP_STRUCTURE_INVALID"


def test_parse_no_md_content_rejected() -> None:
    data = _zip_bytes({"V/1.章/cover.png": "只有图片。", "V/2.章/empty.md": "   "})
    with pytest.raises(AppError) as exc:
        parse_zip_archive(data, settings=_settings())
    assert _error_code(exc.value) == "ZIP_STRUCTURE_INVALID"


def test_parse_corrupt_zip_extract_failed() -> None:
    with pytest.raises(AppError) as exc:
        parse_zip_archive(b"PK\x03\x04 not a real zip", settings=_settings())
    assert _error_code(exc.value) == "ZIP_EXTRACT_FAILED"


def test_parse_non_utf8_md_extract_failed() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("V/1.章/a.md", "正文".encode("gbk"))
    with pytest.raises(AppError) as exc:
        parse_zip_archive(buf.getvalue(), settings=_settings())
    assert _error_code(exc.value) == "ZIP_EXTRACT_FAILED"


def test_decode_name_gbk_recovery() -> None:
    """无 UTF-8 flag 时按 cp437 原始字节 → GBK 恢复中文文件名（中文 Windows zip 常见）。

    zipfile 公开 API 会给非 ASCII 名自动加 UTF-8 flag，无法构造真 GBK zip，
    故直接单测 _decode_name 的恢复分支。
    """
    from services.projects.zip_archive import _decode_name

    info = zipfile.ZipInfo("V/1.图与执行模型/1.1 是什么.md".encode("gbk").decode("cp437"))
    info.flag_bits = 0
    assert _decode_name(info) == "V/1.图与执行模型/1.1 是什么.md"
    # 带 UTF-8 flag 的名字直接信任
    info_utf8 = zipfile.ZipInfo("V/1.图与执行模型.md")
    info_utf8.flag_bits = 0x800
    assert _decode_name(info_utf8) == "V/1.图与执行模型.md"


def test_parse_skips_empty_md_and_empty_chapter() -> None:
    """空 md 跳过；整树无 md 的子文件夹不生成章节。"""
    data = _zip_bytes(
        {"V/1.有料/a.md": "正文。", "V/2.空白/empty.md": "", "V/3.全空/skip.txt": "x"}
    )
    archive = parse_zip_archive(data, settings=_settings())
    assert [c.name for c in archive.chapters] == ["1.有料"]


def test_parse_file_count_limit() -> None:
    files = {f"V/1.章/{i}.md": "x" for i in range(3)}
    with pytest.raises(AppError) as exc:
        parse_zip_archive(_zip_bytes(files), settings=_settings(zip_max_files=2))
    assert _error_code(exc.value) == "ZIP_UPLOAD_INVALID"


def test_parse_total_chars_limit() -> None:
    files = {"V/1.章/a.md": "字" * 11, "V/1.章/b.md": "字" * 10}
    with pytest.raises(AppError) as exc:
        parse_zip_archive(_zip_bytes(files), settings=_settings(zip_max_total_chars=20))
    assert _error_code(exc.value) == "ZIP_UPLOAD_INVALID"


def test_parse_single_member_bomb_capped_by_byte_limit() -> None:
    """单成员超 4×字符帽的字节量直接拒绝，不整体解压（zip 炸弹防线）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("V/1.章/huge.md", "a" * (20 * 4 + 2))  # 帽=20 字符 → 82 字节 > 80
    with pytest.raises(AppError) as exc:
        parse_zip_archive(buf.getvalue(), settings=_settings(zip_max_total_chars=20))
    assert _error_code(exc.value) == "ZIP_UPLOAD_INVALID"


def test_validate_zip_upload_container_checks() -> None:
    settings = _settings(zip_max_size_bytes=100)
    ok: dict[str, Any] = {
        "filename": "notes.zip",
        "content_type": "application/zip",
        "magic": b"PK\x03\x04",
        "size_bytes": 10,
    }
    validate_zip_upload(**ok, settings=settings)  # 通过
    validate_zip_upload(**{**ok, "content_type": "application/octet-stream"}, settings=settings)
    validate_zip_upload(**{**ok, "content_type": "application/x-zip-compressed"}, settings=settings)
    for bad, field in [
        ({**ok, "filename": "notes.pdf"}, "扩展名"),
        ({**ok, "magic": b"%PDF-"}, "文件头"),
        ({**ok, "content_type": "text/plain"}, "MIME"),
        ({**ok, "size_bytes": 101}, "MB 限制"),
    ]:
        with pytest.raises(AppError) as exc:
            validate_zip_upload(**bad, settings=settings)
        assert _error_code(exc.value) == "ZIP_UPLOAD_INVALID"
        assert field in exc.value.message


def test_parse_bom_and_whitespace_stripped() -> None:
    data = _zip_bytes({"V/1.章/a.md": "\ufeff  正文。\n\n"})
    archive = parse_zip_archive(data, settings=_settings())
    assert archive.chapters[0].files[0][1] == "正文。"


def test_natural_key_never_compares_across_types() -> None:
    """排序键安全：混合中英文/数字/符号路径排序不抛 TypeError。"""
    from services.projects.zip_archive import _natural_key

    paths = ["1.开始", "10.末章", "2.基础", "附录", "Z章", "a1", "a10", "a2"]
    assert min(paths, key=_natural_key) == "1.开始"
    assert sorted(["a1", "a10", "a2"], key=_natural_key) == ["a1", "a2", "a10"]
