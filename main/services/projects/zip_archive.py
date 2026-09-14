"""services.projects.zip_archive：ZIP 笔记包解析（V25-D-35，纯函数、无 DB 依赖）。

结构契约（主文件夹 → 章节文件夹 → md 正文，宽容边界）：
- zip 顶层忽略垃圾条目（``__MACOSX``/``.DS_Store``/``Thumbs.db``/``.obsidian``、
  macOS ``._*`` AppleDouble）后必须恰好一个主文件夹，否则 ``ZIP_STRUCTURE_INVALID``；
- 主文件夹的一级子文件夹 = 章节，文件夹内 md 递归收集（相对路径自然排序）；
- 主文件夹下直接放置的 md 收进固定「总览」章节，排在所有章节最前；
- 非 md 文件静默忽略；空 md 跳过；整树无 md 的子文件夹不生成章节；
  全包无有效正文 → ``ZIP_STRUCTURE_INVALID``。

字符帽（``zip_max_total_chars``）按 4 字节/字符折算为成员读取字节上限，
单成员与累计都受其约束（zip 炸弹防线：超限即 ``ZIP_UPLOAD_INVALID``，不整体解压）。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from app.config import Settings
from app.errors import AppError, ErrorCode

# 任意路径段命中即忽略（系统/编辑器垃圾，不参与"恰好一个主文件夹"判定）
_JUNK_SEGMENTS = frozenset({"__MACOSX", ".DS_Store", "Thumbs.db", ".obsidian"})
# MIME 白名单：zip 族 + Android SAF 常见兜底 octet-stream（以魔数+扩展名为准）
_ZIP_MIME = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)

_NUM_SPLIT = re.compile(r"(\d+)")


@dataclass(frozen=True)
class ZipChapter:
    """一章 = 章节名 + 有序 md 文件列表 [(相对路径, 正文)]。"""

    name: str
    files: list[tuple[str, str]]


@dataclass(frozen=True)
class ZipArchive:
    """解析产物：章节序列（「总览」若存在恒排最前）+ md 正文总字符数。"""

    chapters: list[ZipChapter]
    total_chars: int


def validate_zip_upload(
    *,
    filename: str,
    content_type: str,
    magic: bytes,
    size_bytes: int,
    settings: Settings,
) -> None:
    """容器三重校验（V25-D-35）：.zip 扩展名 + PK 魔数 + zip 族 MIME + ≤大小限制。"""
    reasons: list[str] = []
    if not filename.lower().endswith(".zip"):
        reasons.append(f"扩展名非 .zip（{filename!r}）")
    if not magic.startswith(b"PK\x03\x04"):
        reasons.append("文件头非 ZIP（PK）")
    if content_type.lower() not in _ZIP_MIME:
        reasons.append(f"MIME 非 zip 族（{content_type!r}）")
    if size_bytes > settings.zip_max_size_bytes:
        reasons.append(
            f"超过 {settings.zip_max_size_bytes // (1024 * 1024)}MB 限制（{size_bytes} bytes）"
        )
    if reasons:
        raise AppError(ErrorCode.ZIP_UPLOAD_INVALID, "ZIP 文件校验失败：" + "；".join(reasons))


def _natural_key(path: str) -> tuple[str | int, ...]:
    """数字感知排序键：'1.' < '2.' < '10.'；段序列恒为 [str, int, str, ...] 不会跨型比较。"""
    return tuple(int(part) if part.isdigit() else part.lower() for part in _NUM_SPLIT.split(path))


def _decode_name(info: zipfile.ZipInfo) -> str:
    """条目名解码：UTF-8 flag 直接信任；无 flag 时 cp437 原始字节依次尝试 utf-8/gbk 恢复。"""
    if info.flag_bits & 0x800:
        return info.filename
    raw = info.filename.encode("cp437")
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _is_junk(path: str) -> bool:
    segments = path.split("/")
    return any(seg in _JUNK_SEGMENTS or seg.startswith("._") for seg in segments)


def parse_zip_archive(data: bytes, *, settings: Settings) -> ZipArchive:
    """解析 ZIP 笔记包字节 → 章节序列；结构不符/损坏/超限抛对应 AppError。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = [
                (_decode_name(info), info)
                for info in zf.infolist()
                if not info.is_dir() and not _is_junk(_decode_name(info))
            ]
            root_files = [path for path, _ in entries if "/" not in path]
            tops = {path.split("/", 1)[0] for path, _ in entries}
            if root_files or len(tops) != 1:
                raise AppError(
                    ErrorCode.ZIP_STRUCTURE_INVALID,
                    "ZIP 顶层须恰好一个主文件夹（不接受散落根级文件或多个顶层条目）",
                )
            chapters = _collect(zf, entries, main_folder=tops.pop(), settings=settings)
    except zipfile.BadZipFile as exc:
        raise AppError(ErrorCode.ZIP_EXTRACT_FAILED, "ZIP 文件损坏或无法读取") from exc
    total = sum(len(content) for chapter in chapters for _, content in chapter.files)
    if total == 0:
        raise AppError(ErrorCode.ZIP_STRUCTURE_INVALID, "包内没有有效的 Markdown 正文")
    return ZipArchive(chapters=chapters, total_chars=total)


def _collect(
    zf: zipfile.ZipFile,
    entries: list[tuple[str, zipfile.ZipInfo]],
    *,
    main_folder: str,
    settings: Settings,
) -> list[ZipChapter]:
    """按主文件夹归位条目：根级 md → 「总览」，一级子文件夹 → 章节（嵌套递归归入）。"""
    overview: list[tuple[str, str]] = []
    folders: dict[str, list[tuple[str, str]]] = {}
    max_member_bytes = settings.zip_max_total_chars * 4
    total_chars = 0
    md_count = 0
    for path, info in entries:
        rel = path.split("/", 1)[1]
        if not rel or ".." in rel.split("/"):
            continue
        if not path.lower().endswith(".md"):
            continue  # 非 md 静默忽略（图片/杂项）
        md_count += 1
        if md_count > settings.zip_max_files:
            raise AppError(
                ErrorCode.ZIP_UPLOAD_INVALID, f"超过 {settings.zip_max_files} 个 md 文件限制"
            )
        with zf.open(info) as fh:
            raw = fh.read(max_member_bytes + 1)
        if len(raw) > max_member_bytes:
            raise AppError(
                ErrorCode.ZIP_UPLOAD_INVALID,
                f"单个 md 或总量超过 {settings.zip_max_total_chars} 字符限制",
            )
        try:
            content = raw.decode("utf-8").lstrip("\ufeff").strip()
        except UnicodeDecodeError as exc:
            raise AppError(ErrorCode.ZIP_EXTRACT_FAILED, f"md 文件非 UTF-8 编码（{rel}）") from exc
        if not content:
            continue  # 空 md 跳过
        total_chars += len(content)
        if total_chars > settings.zip_max_total_chars:
            raise AppError(
                ErrorCode.ZIP_UPLOAD_INVALID,
                f"md 正文总量超过 {settings.zip_max_total_chars} 字符限制",
            )
        segments = rel.split("/")
        if len(segments) == 1:
            overview.append((rel, content))
        else:
            folders.setdefault(segments[0], []).append((rel, content))
    chapters: list[ZipChapter] = []
    if overview:
        chapters.append(ZipChapter(name="总览", files=sorted(overview, key=_file_key)))
    for name in sorted(folders, key=_natural_key):
        chapters.append(ZipChapter(name=name, files=sorted(folders[name], key=_file_key)))
    return chapters


def _file_key(item: tuple[str, str]) -> tuple[str | int, ...]:
    return _natural_key(item[0])
