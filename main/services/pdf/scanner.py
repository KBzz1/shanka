"""services.pdf.scanner：进程内 DB 驱动 PDF 解析扫描器（契约 4.4 定式）。

状态机：PENDING → PARSING → PARSED / FAILED(error_code)。
- 单进程 MVP：PENDING/PARSING 均视为可处理（进程崩溃后 PARSING 残留，重启重新解析）；
- 重复解析幂等：处理前清理该 file_id 的既有 chapters；
- 失败不删除原始文件（5.1）；FAILED 行不再重试（终态）。
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import Chapter, PdfFile
from services.pdf.parser import parse_pdf

logger = logging.getLogger(__name__)


def validate_upload(
    *,
    filename: str,
    content_type: str,
    magic: bytes,
    size_bytes: int,
    page_count_hint: int | None,
    settings: Settings,
) -> None:
    """三重校验 + 限制（6.1）：魔数/扩展名/MIME + ≤50MB + ≤500 页。"""
    ok_ext = filename.lower().endswith(".pdf")
    ok_magic = magic.startswith(b"%PDF")
    ok_mime = content_type.lower() == "application/pdf"
    ok_size = size_bytes <= settings.pdf_max_size_bytes
    ok_pages = page_count_hint is None or page_count_hint <= settings.pdf_max_pages
    if not (ok_ext and ok_magic and ok_mime and ok_size and ok_pages):
        raise AppError(
            ErrorCode.PDF_UPLOAD_INVALID, "PDF 文件校验失败（扩展名/魔数/MIME/大小/页数）"
        )


def process_pending(session: Session, *, storage: Any) -> int:
    """处理一条可解析行（PENDING 或 PARSING 残留）。返回处理数（0 或 1）。"""
    row = session.scalar(
        select(PdfFile)
        .where(PdfFile.status.in_(["PENDING", "PARSING"]))
        .order_by(PdfFile.created_at)
        .limit(1)
    )
    if row is None:
        return 0
    row.status = "PARSING"
    session.flush()
    try:
        path = storage.open(row.storage_key)
        _text_sample, chapters = parse_pdf(path)  # 文本样例仅确认文本层存在，不落库（AC-08）
        # 幂等：清理既有 chapters 再插入
        for old in session.scalars(select(Chapter).where(Chapter.file_id == row.file_id)).all():
            session.delete(old)
        session.flush()
        for ch in chapters:
            session.add(
                Chapter(
                    chapter_id=str(uuid.uuid4()),
                    file_id=row.file_id,
                    name=ch["name"],
                    start_page=ch["start_page"],
                    end_page=ch["end_page"],
                )
            )
        row.status = "PARSED"
        row.error_code = None
    except AppError as exc:
        row.status = "FAILED"
        row.error_code = exc.code.value
    except Exception:  # noqa: BLE001
        logger.warning("pdf parse unexpected failure", extra={"error_code": "PDF_PARSE_FAILED"})
        row.status = "FAILED"
        row.error_code = "PDF_PARSE_FAILED"
    return 1


def scan_once(session_factory: sessionmaker[Session], *, storage: Any) -> int:
    """扫描一轮：处理全部可解析行（MVP 逐条）。返回处理数。"""
    total = 0
    with session_factory() as session:
        while True:
            n = process_pending(session, storage=storage)
            if n == 0:
                break
            session.commit()
            total += n
    return total
