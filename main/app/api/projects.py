"""学习项目路由（structure-contract 6.2；openapi /projects；V2.5）。handler 只做 HTTP 映射。

- POST /projects 与 POST /{project_id}/replace-pdf：multipart 上传幂等顺序沿用 /pdfs 上传
  定式（文件读取 + 三重校验 + 页数 hint + storage.save 在幂等外，biz 只做 DB 元数据写入）；
  存储补偿（删除/替换时 storage 失败回滚元数据）见 services/projects/service.py。
- DELETE /projects/{project_id}?retain_decks=：两种用户决策（保留或删除全部项目牌组），
  活跃任务/解析中保护；章节删除 ?delete_cards= 同款保护与两决策。
- 幂等：写接口强制 Idempotency-Key 并走 execute_idempotent（契约 1.3）。
- 路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 pdfs/decks 同理）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.pdfs import ChapterUpdateRequest
from app.schemas.project import ProjectStudySettingsUpdateRequest
from app.schemas.projects import ProjectRenameRequest
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from infra.storage.local import LocalStorage
from services.pdf.parser import page_count_hint
from services.pdf.scanner import validate_upload
from services.pdf.service import chapter_view
from services.projects.service import (
    confirm_chapters,
    create_project,
    delete_project,
    delete_project_chapter,
    get_project,
    get_study_settings,
    list_projects,
    rename_project,
    replace_pdf,
    update_project_chapter,
    update_study_settings,
)

router = APIRouter(prefix="/projects", tags=["projects"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


async def _upload(request: Request, file: UploadFile) -> tuple[bytes, str]:
    """上传共用段（POST /projects 与 replace-pdf）：校验 → 存盘 → 返回 (data, storage_key)。

    与 /pdfs 上传同款：校验与存储写入在幂等外（handler 异步部分），biz 只做 DB 元数据写入。
    """
    settings: Settings = request.app.state.settings
    data = await file.read()
    validate_upload(
        filename=file.filename or "",
        content_type=file.content_type or "",
        magic=data[:5],
        size_bytes=len(data),
        page_count_hint=page_count_hint(data),
        settings=settings,
    )
    storage: LocalStorage = request.app.state.storage
    return data, storage.save(data)


@router.post("", status_code=201)
async def create_project_endpoint(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
    name: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    """上传 PDF 建立学习项目（可选 name；缺省取文件名去扩展名；上传成功即建立）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = request.url.path
    data, storage_key = await _upload(request, file)
    body_hash = request_body_hash(data)  # multipart 幂等 body 比对：文件内容 hash

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        project = create_project(
            session,
            user_id=user_id,
            filename=file.filename or "upload.pdf",
            size_bytes=len(data),
            storage_key=storage_key,
            now=_now(),
            name=name,
        )
        session.flush()
        return 201, project

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


@router.get("", status_code=200)
def list_projects_endpoint(
    request: Request, session: Annotated[Session, Depends(get_db_session)]
) -> JSONResponse:
    items = list_projects(session, user_id=request.state.principal.user_id)
    return JSONResponse(status_code=200, content={"items": items})


@router.get("/{project_id}", status_code=200)
def get_project_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    body = get_project(session, user_id=request.state.principal.user_id, project_id=project_id)
    return JSONResponse(status_code=200, content=body)


@router.patch("/{project_id}", status_code=200)
def rename_project_endpoint(
    request: Request,
    project_id: str,
    payload: ProjectRenameRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = rename_project(
            session, user_id=user_id, project_id=project_id, name=payload.name, now=_now()
        )
        return 200, body

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


@router.delete("/{project_id}", status_code=204)
def delete_project_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    retain_decks: Annotated[bool, Query()],
) -> Response:
    """删除项目（活跃任务/解析中保护；retain_decks 选择保留或删除全部项目牌组）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_project(
            session,
            user_id=user_id,
            project_id=project_id,
            retain_decks=retain_decks,
            storage=request.app.state.storage,
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


@router.post("/{project_id}/replace-pdf", status_code=200)
async def replace_pdf_endpoint(
    request: Request,
    project_id: str,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """仅解析失败项目可替换并重新解析（原子替换 PDF）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/replace-pdf"
    data, storage_key = await _upload(request, file)
    body_hash = request_body_hash(data)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = replace_pdf(
            session,
            user_id=user_id,
            project_id=project_id,
            filename=file.filename or "upload.pdf",
            size_bytes=len(data),
            storage_key=storage_key,
            now=_now(),
            storage=request.app.state.storage,
        )
        return 200, body

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


@router.patch("/{project_id}/chapters/{chapter_id}", status_code=200)
def patch_project_chapter_endpoint(
    request: Request,
    project_id: str,
    chapter_id: str,
    payload: ChapterUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        chapter = update_project_chapter(
            session,
            user_id=user_id,
            project_id=project_id,
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


@router.delete("/{project_id}/chapters/{chapter_id}", status_code=204)
def delete_project_chapter_endpoint(
    request: Request,
    project_id: str,
    chapter_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    delete_cards: Annotated[bool, Query()] = False,
) -> Response:
    """删除章节（活跃任务保护；保留卡时 chapter_id 置空，delete_cards 选择同时删卡）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/chapters/{chapter_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_project_chapter(
            session,
            user_id=user_id,
            project_id=project_id,
            chapter_id=chapter_id,
            delete_cards=delete_cards,
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


@router.post("/{project_id}/confirm-chapters", status_code=200)
def confirm_chapters_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """确认目录，使项目进入 READY（未解析/已确认 → 409 PROJECT_STATE_CONFLICT）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/confirm-chapters"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = confirm_chapters(session, user_id=user_id, project_id=project_id, now=_now())
        return 200, body

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


@router.get("/{project_id}/study-settings", status_code=200)
def get_study_settings_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    now: str = _now()
    body = get_study_settings(
        session, user_id=request.state.principal.user_id, project_id=project_id, now=now
    )
    # get-or-create 是物化写：首次访问落默认行须提交（依赖 teardown 只 close 不 commit）
    session.commit()
    return JSONResponse(status_code=200, content=body)


@router.patch("/{project_id}/study-settings", status_code=200)
def patch_study_settings_endpoint(
    request: Request,
    project_id: str,
    payload: ProjectStudySettingsUpdateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/study-settings"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = update_study_settings(
            session,
            user_id=user_id,
            project_id=project_id,
            payload=payload.model_dump(exclude_unset=True),
            now=_now(),
        )
        return 200, body

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
