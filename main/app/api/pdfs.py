"""PDF 路由（structure-contract 6.1；openapi /pdfs）——V2.5 过渡期兼容路径。

委托语义（6.2 注）：POST /pdfs 委托项目创建（services.projects.create_project，
上传同时建立学习项目——同一业务模型，无第二套项目/任务状态）；DELETE /pdfs/{file_id}
经 services.pdf.service.delete_pdf 委托项目删除（retain_decks=true）；GET/章节路由
读同一数据模型（视图构建单一来源 services.pdf.service）。V2.5 Release 客户端只使用
项目接口。handler 只做 HTTP 映射。

multipart 上传幂等顺序（Task 4 报告）：文件读取 + 三重校验 + 页数 hint +
storage.save 在 handler 异步部分完成（execute_idempotent 之前）；biz 只做 DB
元数据插入。同 key 重复上传会重复 save（孤儿文件，MVP 接受，由存储清理兜底）。
路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 decks 同理）。
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
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
from infra.db.models import Chapter
from infra.db.session import format_utc, get_db_session
from infra.storage.local import LocalStorage
from services.pdf.parser import page_count_hint
from services.pdf.scanner import validate_upload
from services.pdf.service import (
    chapter_view,
    delete_chapter,
    delete_pdf,
    get_pdf,
    list_pdfs,
    pdf_view,
    update_chapter,
)
from services.projects.service import create_project

router = APIRouter(prefix="/pdfs", tags=["pdf"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.post("", status_code=201, response_model=PdfFileSchema)
async def upload_pdf_endpoint(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = request.url.path
    settings: Settings = request.app.state.settings
    data = await file.read()
    # 2026-08-11 联调诊断：记录上传特征（不记录 PDF 内容，红线 8.1）
    logging.getLogger("app.api.pdfs").info(
        "pdf upload received",
        extra={
            "request_id": getattr(request.state, "request_id", ""),
            "user_id": request.state.principal.user_id,
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
        page_count_hint=page_count_hint(data),
        settings=settings,
    )
    storage: LocalStorage = request.app.state.storage
    storage_key = storage.save(data)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        # 兼容路径委托项目创建（6.2）：上传同时建立学习项目；响应仍为 PdfFile（旧契约载荷）
        project = create_project(
            session,
            user_id=user_id,
            filename=file.filename or "upload.pdf",
            size_bytes=len(data),
            storage_key=storage_key,
            now=_now(),
        )
        session.flush()
        return 201, project["file"]

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
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
    items = [pdf_view(pdf) for pdf in list_pdfs(session, user_id=request.state.principal.user_id)]
    return JSONResponse(content={"items": items})


@router.get("/{file_id}", response_model=PdfFileSchema)
def get_pdf_endpoint(
    request: Request,
    file_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    pdf = get_pdf(session, user_id=request.state.principal.user_id, file_id=file_id)
    # 决策：详情总是返回 chapters 字段（PARSED 时查 Chapter 表；否则 None）
    chapters: list[dict[str, Any]] | None = None
    if pdf.status == "PARSED":
        rows = session.scalars(
            select(Chapter).where(Chapter.file_id == file_id).order_by(Chapter.start_page)
        ).all()
        chapters = [chapter_view(ch) for ch in rows]
    return JSONResponse(content=pdf_view(pdf, chapters))


@router.delete("/{file_id}", status_code=204)
def delete_pdf_endpoint(
    request: Request,
    file_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    abandon_pre_generation_tasks: Annotated[bool, Query()] = False,
    cancel_active_tasks: Annotated[bool, Query()] = False,
) -> Response:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = (
        f"/pdfs/{file_id}?abandon_pre_generation_tasks={str(abandon_pre_generation_tasks).lower()}"
        f"&cancel_active_tasks={str(cancel_active_tasks).lower()}"
    )
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_pdf(
            session,
            user_id=user_id,
            file_id=file_id,
            storage=request.app.state.storage,
            abandon_pre_generation_tasks=abandon_pre_generation_tasks,
            cancel_active_tasks=cancel_active_tasks,
            now=_now(),
        )
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        user_id=user_id,
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
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/pdfs/{file_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_chapter(session, user_id=user_id, file_id=file_id, chapter_id=chapter_id)
        return 204, {}

    _replayed, status, _body = execute_idempotent(
        session,
        user_id=user_id,
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
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/pdfs/{file_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        chapter = update_chapter(
            session,
            user_id=user_id,
            file_id=file_id,
            chapter_id=chapter_id,
            name=payload.name,
            start_page=payload.start_page,
            end_page=payload.end_page,
            now=_now(),
        )
        session.flush()
        return 200, chapter_view(chapter)

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=body_hash,
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
