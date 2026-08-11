"""PDF 路由（structure-contract 6.1；openapi /pdfs）。handler 只做 HTTP 映射。

multipart 上传幂等顺序（Task 4 报告）：文件读取 + 三重校验 + 页数 hint +
storage.save 在 handler 异步部分完成（execute_idempotent 之前）；biz 只做 DB
元数据插入。同 key 重复上传会重复 save（孤儿文件，MVP 接受，由存储清理兜底）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 decks 同理）。
"""

import logging
from io import BytesIO
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.pdfs import Chapter as ChapterSchema
from app.schemas.pdfs import ChapterUpdateRequest
from app.schemas.pdfs import PdfFile as PdfFileSchema
from infra.clock import SystemClock
from infra.db.models import Chapter, PdfFile
from infra.db.session import format_utc, get_db_session
from infra.storage.local import LocalStorage
from services.pdf.scanner import validate_upload
from services.pdf.service import (
    delete_chapter,
    delete_pdf,
    get_pdf,
    list_pdfs,
    update_chapter,
    upload_pdf,
)

router = APIRouter(prefix="/pdfs", tags=["pdf"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


def _page_count_hint(data: bytes) -> int | None:
    """页数 hint（carry-forward 决策）：pypdf 快速读页数；损坏文件 → None（扫描器 FAILED 兜底）。"""
    try:
        return len(PdfReader(BytesIO(data)).pages)
    except Exception:  # noqa: BLE001  # 损坏/非 PDF 一律无 hint，上传校验与扫描器兜底
        return None


def _chapter_view(chapter: Chapter) -> dict[str, Any]:
    return {
        "chapter_id": chapter.chapter_id,
        "name": chapter.name,
        "start_page": chapter.start_page,
        "end_page": chapter.end_page,
    }


def _pdf_view(pdf: PdfFile, chapters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "file_id": pdf.file_id,
        "filename": pdf.filename,
        "size_bytes": pdf.size_bytes,
        "status": pdf.status,
        "error_code": pdf.error_code,
        "chapters": chapters,
        "created_at": pdf.created_at,
    }


@router.post("", status_code=201, response_model=PdfFileSchema)
async def upload_pdf_endpoint(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    settings: Settings = request.app.state.settings
    data = await file.read()
    # 2026-08-11 联调诊断：记录上传特征（不记录 PDF 内容，红线 8.1）
    logging.getLogger("app.api.pdfs").info(
        "pdf upload received",
        extra={
            "request_id": getattr(request.state, "request_id", ""),
            "device_id": request.state.device_id,
            "size_bytes": len(data),
            "content_type": file.content_type or "",
            "file_name": file.filename or "",
        },
    )
    body_hash = request_body_hash(data)  # multipart 幂等 body 比对：文件内容 hash
    # 校验与存储写入在幂等外（handler 异步部分）：biz 只做 DB 元数据插入
    validate_upload(
        filename=file.filename or "",
        content_type=file.content_type or "",
        magic=data[:5],
        size_bytes=len(data),
        page_count_hint=_page_count_hint(data),
        settings=settings,
    )
    storage: LocalStorage = request.app.state.storage
    storage_key = storage.save(data)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        pdf = upload_pdf(
            session,
            device_id=device_id,
            filename=file.filename or "upload.pdf",
            size_bytes=len(data),
            storage_key=storage_key,
            now=_now(),
        )
        session.flush()
        return 201, _pdf_view(pdf)

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("", response_model=dict[str, list[PdfFileSchema]])
def list_pdfs_endpoint(
    request: Request, session: Annotated[Session, Depends(get_db_session)]
) -> JSONResponse:
    items = [_pdf_view(pdf) for pdf in list_pdfs(session, device_id=request.state.device_id)]
    return JSONResponse(content={"items": items})


@router.get("/{file_id}", response_model=PdfFileSchema)
def get_pdf_endpoint(
    request: Request,
    file_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    pdf = get_pdf(session, device_id=request.state.device_id, file_id=file_id)
    # 决策：详情总是返回 chapters 字段（PARSED 时查 Chapter 表；否则 None）
    chapters: list[dict[str, Any]] | None = None
    if pdf.status == "PARSED":
        rows = session.scalars(
            select(Chapter).where(Chapter.file_id == file_id).order_by(Chapter.start_page)
        ).all()
        chapters = [_chapter_view(ch) for ch in rows]
    return JSONResponse(content=_pdf_view(pdf, chapters))


@router.delete("/{file_id}", status_code=204)
def delete_pdf_endpoint(
    request: Request,
    file_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/pdfs/{file_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_pdf(session, device_id=device_id, file_id=file_id, storage=request.app.state.storage)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return Response(status_code=status)


@router.delete("/{file_id}/chapters/{chapter_id}", status_code=204)
def delete_chapter_endpoint(
    request: Request,
    file_id: str,
    chapter_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> Response:
    """删除章节（structure-contract 6.1）；关联知识点 chapter_id 置 null。"""
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/pdfs/{file_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_chapter(session, device_id=device_id, file_id=file_id, chapter_id=chapter_id)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return Response(status_code=status)


@router.patch("/{file_id}/chapters/{chapter_id}", response_model=ChapterSchema)
def patch_chapter_endpoint(
    request: Request,
    file_id: str,
    chapter_id: str,
    payload: ChapterUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/pdfs/{file_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        chapter = update_chapter(
            session,
            device_id=device_id,
            file_id=file_id,
            chapter_id=chapter_id,
            name=payload.name,
            start_page=payload.start_page,
            end_page=payload.end_page,
            now=_now(),
        )
        session.flush()
        return 200, _chapter_view(chapter)

    _replayed, status, body = execute_idempotent(
        session,
        device_id=device_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
