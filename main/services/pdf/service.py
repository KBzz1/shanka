"""services.pdf.service：PDF 用例（上传/列表/详情/删除/章节 PATCH）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制。
删除保护与存储清理：元数据删除 + storage.delete 在同一调用；storage 清理失败
记录 WARN 不阻断（元数据删除后孤儿文件由运维清理，MVP 接受）。
"""

import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Chapter, KnowledgePoint, PdfFile, Task
from infra.storage.local import LocalStorage

_NON_TERMINAL = ["PENDING", "RUNNING", "PAUSED"]

logger = logging.getLogger(__name__)


def _uuid4() -> str:
    return str(uuid.uuid4())


def _owned_pdf(session: Session, *, user_id: str, file_id: str) -> PdfFile:
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.user_id != user_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    return pdf


def upload_pdf(
    session: Session, *, user_id: str, filename: str, size_bytes: int, storage_key: str, now: str
) -> PdfFile:
    """上传登记：落 PENDING，解析由扫描器接管（Task 3）。"""
    pdf = PdfFile(
        file_id=_uuid4(),
        user_id=user_id,
        filename=filename,
        storage_key=storage_key,
        size_bytes=size_bytes,
        status="PENDING",
        created_at=now,
    )
    session.add(pdf)
    return pdf


def list_pdfs(session: Session, *, user_id: str) -> list[PdfFile]:
    return list(
        session.scalars(
            select(PdfFile).where(PdfFile.user_id == user_id).order_by(PdfFile.created_at.desc())
        ).all()
    )


def get_pdf(session: Session, *, user_id: str, file_id: str) -> PdfFile:
    return _owned_pdf(session, user_id=user_id, file_id=file_id)


def delete_pdf(session: Session, *, user_id: str, file_id: str, storage: LocalStorage) -> None:
    pdf = _owned_pdf(session, user_id=user_id, file_id=file_id)
    blocking = (
        session.scalar(
            select(func.count(Task.task_id)).where(
                Task.file_id == file_id,
                Task.user_id == user_id,  # 一致性守卫（DESIGN §5.1）：只计本用户任务
                Task.status.in_(_NON_TERMINAL),
            )
        )
        or 0
    )
    if blocking:
        raise AppError(ErrorCode.TASK_IN_PROGRESS, "存在进行中的任务引用该文件")
    # 终态任务 file_id SET NULL（database-design §3：tasks.file_id ON DELETE SET NULL）
    for task in session.scalars(
        select(Task).where(Task.file_id == file_id, Task.user_id == user_id)
    ).all():
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
    user_id: str,
    file_id: str,
    chapter_id: str,
    name: str | None,
    start_page: int | None,
    end_page: int | None,
    now: str,
) -> Chapter:
    """章节 PATCH（章节确认流程，仅 PARSED 后可用；非 PARSED → 409 TASK_STATE_CONFLICT）。

    部分更新（fix round 1，openapi「至少提供一个字段;未提供的字段保持不变」）：
    全 None → VALIDATION_ERROR；仅更新非 None 字段；应用后校验 start <= end。
    """
    if name is None and start_page is None and end_page is None:
        raise AppError(ErrorCode.VALIDATION_ERROR, "至少提供一个字段")
    if start_page is not None and start_page < 1:
        raise AppError(ErrorCode.VALIDATION_ERROR, "起始页码非法")
    if end_page is not None and end_page < 1:
        raise AppError(ErrorCode.VALIDATION_ERROR, "结束页码非法")
    pdf = _owned_pdf(session, user_id=user_id, file_id=file_id)
    if pdf.status != "PARSED":
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "PDF 尚未解析完成")
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or chapter.file_id != file_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "章节不存在")
    if name is not None:
        chapter.name = name
    if start_page is not None:
        chapter.start_page = start_page
    if end_page is not None:
        chapter.end_page = end_page
    if chapter.start_page > chapter.end_page:
        raise AppError(ErrorCode.VALIDATION_ERROR, "章节页码范围非法")
    return chapter


def delete_chapter(session: Session, *, user_id: str, file_id: str, chapter_id: str) -> None:
    """删除章节（structure-contract 6.1，契约 3.6 语义落地）。

    仅 PARSED 后可删（同章节 PATCH 约束）；关联 knowledge_points.chapter_id 应用层
    置 null（2.6 无 DB FK）；历史任务 selected_chapters 为快照，不受影响。
    """
    pdf = _owned_pdf(session, user_id=user_id, file_id=file_id)
    if pdf.status != "PARSED":
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "PDF 尚未解析完成")
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or chapter.file_id != file_id:
        raise AppError(ErrorCode.CHAPTER_NOT_FOUND, "章节不存在")
    session.execute(
        update(KnowledgePoint)
        .where(KnowledgePoint.chapter_id == chapter_id)
        .values(chapter_id=None)
    )
    session.delete(chapter)
