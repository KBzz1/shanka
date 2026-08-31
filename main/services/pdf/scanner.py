"""services.pdf.scanner：进程内 DB 驱动 PDF 解析扫描器（契约 4.4 定式）。

状态机：PENDING → PARSING(短租约) → PARSED / FAILED(error_code)。
- 领取阶段只持有短数据库事务；解析耗时在事务外进行，租约过期后可被另一 worker 接管；
- 删除/替换会递增 parse_version 并清除租约，迟到结果通过 token/version 栅栏丢弃；
- 重复解析幂等：发布前清理该 file_id 的既有 chapters 与 text_chunks 再重建；
- PARSED 时完整页文本一页一行落 text_chunks（spec §4.1，与章节解耦）；
- 失败不删除原始文件（5.1）；FAILED 行不再重试（终态）。
"""

import logging
import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import Chapter, PdfFile
from infra.db.session import format_utc
from services.pdf.parser import extract_pages, parse_pdf
from services.pdf.text_chunks import persist_text_chunks

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
    """三重校验 + 限制（6.1）：魔数/扩展名/MIME + ≤50MB + ≤500 页。

    失败 message 细分到具体条件（2026-08-11 联调诊断：区分 400 的具体原因，
    前端按 message/localization_key 提示，日志侧由请求日志 error_code 记录）。
    """
    ok_ext = filename.lower().endswith(".pdf")
    ok_magic = magic.startswith(b"%PDF")
    ok_mime = content_type.lower() == "application/pdf"
    ok_size = size_bytes <= settings.pdf_max_size_bytes
    ok_pages = page_count_hint is None or page_count_hint <= settings.pdf_max_pages
    if not (ok_ext and ok_magic and ok_mime and ok_size and ok_pages):
        reasons = []
        if not ok_ext:
            reasons.append(f"扩展名非 .pdf（{filename!r}）")
        if not ok_magic:
            reasons.append("文件头非 %PDF")
        if not ok_mime:
            reasons.append(f"MIME 非 application/pdf（{content_type!r}）")
        if not ok_size:
            reasons.append(
                f"超过 {settings.pdf_max_size_bytes // (1024 * 1024)}MB 限制（{size_bytes} bytes）"
            )
        if not ok_pages:
            reasons.append(f"超过 {settings.pdf_max_pages} 页限制")
        raise AppError(ErrorCode.PDF_UPLOAD_INVALID, "PDF 文件校验失败：" + "；".join(reasons))


def process_pending(session: Session, *, storage: Any) -> int:
    """领取并处理一条 PDF；解析耗时阶段不持有数据库事务。

    ``parse_lease_token`` 和 ``parse_version`` 组成发布栅栏：删除/替换 PDF 后，旧扫描器
    即便已经读完文件，也只能丢弃结果，不能把章节或文本块写回新状态。
    """
    now_dt = SystemClock().now_utc()
    now = format_utc(now_dt)
    lease_until = format_utc(now_dt + timedelta(minutes=10))
    row = session.scalar(
        select(PdfFile)
        .where(
            PdfFile.status.in_(["PENDING", "PARSING"]),
            (PdfFile.parse_lease_until.is_(None) | (PdfFile.parse_lease_until <= now)),
        )
        .order_by(PdfFile.created_at)
        .limit(1)
    )
    if row is None:
        return 0
    file_id = row.file_id
    storage_key = row.storage_key
    lease_token = str(uuid.uuid4())
    parse_version = int(row.parse_version) + 1
    # The initial read only chooses a candidate.  Claim it with a conditional UPDATE so two
    # workers that observe the same expired lease cannot both parse/publish the file.
    claimed = cast(
        CursorResult[Any],
        session.execute(
            update(PdfFile)
            .where(
                PdfFile.file_id == file_id,
                PdfFile.status.in_(["PENDING", "PARSING"]),
                (PdfFile.parse_lease_until.is_(None) | (PdfFile.parse_lease_until <= now)),
            )
            .values(
                status="PARSING",
                parse_lease_token=lease_token,
                parse_lease_until=lease_until,
                parse_version=PdfFile.parse_version + 1,
            )
        ),
    )
    if claimed.rowcount != 1:
        session.rollback()
        return 0
    # 先提交领取，释放写事务，让项目删除可以在解析期间完成。
    session.commit()
    try:
        path = storage.open(storage_key)
        _text_sample, chapters = parse_pdf(path)
        pages = extract_pages(path)
        current = session.get(PdfFile, file_id)
        if (
            current is None
            or current.status != "PARSING"
            or current.parse_lease_token != lease_token
            or current.parse_version != parse_version
        ):
            session.rollback()
            logger.info("pdf parse result discarded", extra={"file_id": file_id})
            return 1
        persist_text_chunks(session, file_id=file_id, pages=pages, now=now)
        for old in session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all():
            session.delete(old)
        session.flush()
        for ch in chapters:
            session.add(
                Chapter(
                    chapter_id=str(uuid.uuid4()),
                    file_id=file_id,
                    material_id=file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                    name=ch["name"],
                    start_page=ch["start_page"],
                    end_page=ch["end_page"],
                )
            )
        # Publish through a conditional UPDATE, not only an in-memory object assignment.  A
        # project deletion/replacement may have committed after the pre-publish read; the fence
        # then affects zero rows and all parsed chunks/chapters are rolled back as stale output.
        published = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="PARSED",
                    error_code=None,
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if published.rowcount != 1:
            session.rollback()
            logger.info("pdf parse result discarded", extra={"file_id": file_id})
            return 1
    except AppError as exc:
        logger.warning(
            "pdf parse failed",
            extra={"error_code": exc.code.value, "error_message": str(exc), "file_id": file_id},
        )
        session.rollback()
        failed = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="FAILED",
                    error_code=exc.code.value,
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if failed.rowcount != 1:
            session.rollback()
    except Exception:  # noqa: BLE001
        logger.warning("pdf parse unexpected failure", extra={"error_code": "PDF_PARSE_FAILED"})
        session.rollback()
        failed = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="FAILED",
                    error_code="PDF_PARSE_FAILED",
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if failed.rowcount != 1:
            session.rollback()
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
