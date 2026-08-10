"""services.pdf.service：PDF 用例（上传/列表/详情/删除/章节 PATCH）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制。
删除保护与存储清理：元数据删除 + storage.delete 在同一调用；storage 清理失败
记录 WARN 不阻断（元数据删除后孤儿文件由运维清理，MVP 接受）。
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Chapter, PdfFile, Task
from infra.storage.local import LocalStorage

_NON_TERMINAL = ["PENDING", "RUNNING", "PAUSED"]

logger = logging.getLogger(__name__)


def _uuid4() -> str:
    return str(uuid.uuid4())


def _owned_pdf(session: Session, *, device_id: str, file_id: str) -> PdfFile:
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    return pdf


def upload_pdf(
    session: Session, *, device_id: str, filename: str, size_bytes: int, storage_key: str, now: str
) -> PdfFile:
    """上传登记：落 PENDING，解析由扫描器接管（Task 3）。"""
    pdf = PdfFile(
        file_id=_uuid4(),
        device_id=device_id,
        filename=filename,
        storage_key=storage_key,
        size_bytes=size_bytes,
        status="PENDING",
        created_at=now,
    )
    session.add(pdf)
    return pdf


def list_pdfs(session: Session, *, device_id: str) -> list[PdfFile]:
    return list(
        session.scalars(
            select(PdfFile)
            .where(PdfFile.device_id == device_id)
            .order_by(PdfFile.created_at.desc())
        ).all()
    )


def get_pdf(session: Session, *, device_id: str, file_id: str) -> PdfFile:
    return _owned_pdf(session, device_id=device_id, file_id=file_id)


def delete_pdf(session: Session, *, device_id: str, file_id: str, storage: LocalStorage) -> None:
    pdf = _owned_pdf(session, device_id=device_id, file_id=file_id)
    blocking = (
        session.scalar(
            select(func.count(Task.task_id)).where(
                Task.file_id == file_id, Task.status.in_(_NON_TERMINAL)
            )
        )
        or 0
    )
    if blocking:
        raise AppError(ErrorCode.TASK_IN_PROGRESS, "存在进行中的任务引用该文件")
    # 终态任务 file_id SET NULL（database-design §3：tasks.file_id ON DELETE SET NULL）
    for task in session.scalars(select(Task).where(Task.file_id == file_id)).all():
        task.file_id = None
    session.delete(pdf)
    try:
        storage.delete(pdf.storage_key)
    except Exception:  # noqa: BLE001
        logger.warning(
            "PDF 存储对象清理失败（孤儿文件由运维清理）", extra={"error_code": "INTERNAL_ERROR"}
        )


def update_chapter(
    session: Session,
    *,
    device_id: str,
    file_id: str,
    chapter_id: str,
    name: str,
    start_page: int,
    end_page: int,
    now: str,
) -> Chapter:
    """章节 PATCH（章节确认流程，仅 PARSED 后可用；非 PARSED → 409 TASK_STATE_CONFLICT）。"""
    pdf = _owned_pdf(session, device_id=device_id, file_id=file_id)
    if pdf.status != "PARSED":
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "PDF 尚未解析完成")
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or chapter.file_id != file_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "章节不存在")
    if start_page < 1 or end_page < start_page:
        raise AppError(ErrorCode.VALIDATION_ERROR, "章节页码范围非法")
    chapter.name = name
    chapter.start_page = start_page
    chapter.end_page = end_page
    return chapter
