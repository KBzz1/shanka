"""学习项目路由（structure-contract 6.2；openapi /projects；V2.5）。handler 只做 HTTP 映射。

- POST /projects/{project_id}/materials/pdf 与 material replace：multipart 上传幂等顺序沿用
  V1 PDF 上传定式（文件读取 + 三重校验 + 页数 hint + storage.save 在幂等外，biz 只做
  DB 元数据写入）；存储补偿（删除/替换时 storage 失败回滚元数据）见 services/projects/service.py。
- DELETE /projects/{project_id}?retain_decks=：两种用户决策（保留或删除全部项目牌组）；
  活跃任务由服务端自动取消，解析中项目由版本栅栏安全删除；章节删除 ?delete_cards= 同款
  保护与两决策。
- 幂等：写接口强制 Idempotency-Key 并走 execute_idempotent（契约 1.3）。
- 路径无 /v1 前缀：/v1 语义由部署层 openapi servers url 承担（与 decks 同理）。
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.middleware.idempotency import (
    execute_idempotent,
    get_idempotency_key,
    request_body_hash,
)
from app.schemas.decks import Deck
from app.schemas.deletion import DeletionPreflight
from app.schemas.pdfs import ChapterUpdateRequest
from app.schemas.progress import ProgressSummary, ProjectWeeklyStats
from app.schemas.project import ProjectStudySettingsUpdateRequest
from app.schemas.projects import (
    ProjectCreateRequest as ProjectCreateRequestAlias,
)
from app.schemas.projects import ProjectRenameRequest, TextMaterialCreateRequest
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from infra.storage.local import LocalStorage
from services.decks.service import attach_deck_to_project, get_deck
from services.pdf.parser import page_count_hint
from services.pdf.scanner import validate_upload
from services.pdf.service import chapter_view
from services.progress.service import project_progress, project_weekly_stats
from services.projects.service import (
    add_pdf_material,
    add_text_material,
    confirm_chapters,
    create_project,
    delete_material,
    delete_project,
    delete_project_chapter,
    get_project,
    get_study_settings,
    list_materials,
    list_projects,
    material_deletion_preflight,
    project_deletion_preflight,
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
def create_project_endpoint(
    request: Request,
    payload: ProjectCreateRequestAlias,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """建立空项目（两步创建第一步，V25-D-29）；资料经 materials 端点添加。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = request.url.path

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        project = create_project(session, user_id=user_id, name=payload.name, now=_now())
        session.flush()
        return 201, project

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=request_body_hash(payload.model_dump_json().encode()),
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


@router.get("/{project_id}/progress", response_model=ProgressSummary)
def project_progress_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    body = project_progress(
        session,
        user_id=request.state.principal.user_id,
        project_id=project_id,
        now=_now(),
    )
    return JSONResponse(status_code=200, content=body)


@router.get("/{project_id}/stats/weekly", response_model=ProjectWeeklyStats)
def project_weekly_stats_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    body = project_weekly_stats(
        session,
        user_id=request.state.principal.user_id,
        project_id=project_id,
        now=SystemClock().now_utc(),
    )
    session.commit()
    return JSONResponse(status_code=200, content=body)


@router.post("/{project_id}/decks/{deck_id}/attach", response_model=Deck)
def attach_deck_endpoint(
    request: Request,
    project_id: str,
    deck_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    user_id = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/decks/{deck_id}/attach"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    now = _now()

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        attach_deck_to_project(
            session, user_id=user_id, project_id=project_id, deck_id=deck_id, now=now
        )
        return 200, get_deck(session, user_id=user_id, deck_id=deck_id, now=now)

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


@router.get("/{project_id}/materials", status_code=200)
def list_materials_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """资料列表（各自状态；TEXT 附单章节）。"""
    user_id: str = request.state.principal.user_id
    items = list_materials(session, user_id=user_id, project_id=project_id)
    return JSONResponse(status_code=200, content={"items": items})


@router.post("/{project_id}/materials/pdf", status_code=201)
async def add_pdf_material_endpoint(
    request: Request,
    project_id: str,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """添加 PDF 资料（≤100MB、≤1000 页；异步解析；重置章节确认，V25-D-31）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/materials/pdf"
    data, storage_key = await _upload(request, file)
    body_hash = request_body_hash(data)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = add_pdf_material(
            session,
            user_id=user_id,
            project_id=project_id,
            filename=file.filename or "upload.pdf",
            size_bytes=len(data),
            storage_key=storage_key,
            now=_now(),
        )
        session.flush()
        return 201, body

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


@router.post("/{project_id}/materials/text", status_code=201)
def add_text_material_endpoint(
    request: Request,
    project_id: str,
    payload: TextMaterialCreateRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """添加粘贴文本资料（≤30000 字；单章节+段落多 chunk；即时就绪，V25-D-32）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/materials/text"

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = add_text_material(
            session,
            user_id=user_id,
            project_id=project_id,
            name=payload.name,
            content=payload.content,
            now=_now(),
            settings=request.app.state.settings,
        )
        session.flush()
        return 201, body

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=request_body_hash(payload.model_dump_json().encode()),
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get(
    "/{project_id}/materials/{material_id}/deletion-preflight", response_model=DeletionPreflight
)
def material_deletion_preflight_endpoint(
    request: Request,
    project_id: str,
    material_id: str,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """资料删除确认页预检（PRD V25-GEN-FR-02）：只读返回将影响的卡片数量。

    引用该资料的活跃任务在删除时由服务端静默取消（V25-D-30，不向用户暴露
    任务选项），故无 blocker 语义，计数仅作展示提示；DELETE 在自身写事务内
    重复检查，客户端不得把预检当预留。"""
    body = material_deletion_preflight(
        session,
        user_id=request.state.principal.user_id,
        project_id=project_id,
        material_id=material_id,
    )
    return JSONResponse(content=body)


@router.delete("/{project_id}/materials/{material_id}", status_code=200)
def delete_material_endpoint(
    request: Request,
    project_id: str,
    material_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    retain_cards: Annotated[bool, Query()] = True,
) -> JSONResponse:
    """删除资料（V25-D-30）：静默取消引用该资料的活跃任务并 fencing；retain_cards
    选择保留或一并删除该资料产出卡片；删最后一份资料后项目转 EMPTY。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/materials/{material_id}"

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = delete_material(
            session,
            user_id=user_id,
            project_id=project_id,
            material_id=material_id,
            retain_cards=retain_cards,
            storage=request.app.state.storage,
            now=_now(),
        )
        session.flush()
        return 200, body

    _replayed, status, body = execute_idempotent(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=key,
        request_body_hash=request_body_hash(f"retain_cards={retain_cards}".encode()),
        fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("/{project_id}/deletion-preflight", response_model=DeletionPreflight)
def project_deletion_preflight_endpoint(
    request: Request,
    project_id: str,
    session: Annotated[Session, Depends(get_db_session)],
    retain_decks: Annotated[bool, Query()] = True,
    cancel_active_tasks: Annotated[bool, Query()] = False,
) -> JSONResponse:
    """Return the current deletion impact and task blockers without changing state.

    The DELETE request repeats this check in its own write transaction; clients must not treat a
    preflight as a reservation.
    """
    body = project_deletion_preflight(
        session,
        user_id=request.state.principal.user_id,
        project_id=project_id,
        retain_decks=retain_decks,
        allow_cancel=cancel_active_tasks,
    )
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
    retain_decks: Annotated[bool, Query()] = True,
) -> Response:
    """删除项目；retain_decks 是唯一业务选择，活跃任务由服务端自动取消。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    # Query choices are part of the idempotent operation.  A reused key must never replay a
    # retain-decks decision with different destructive semantics.
    path = f"/projects/{project_id}?retain_decks={str(retain_decks).lower()}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_project(
            session,
            user_id=user_id,
            project_id=project_id,
            retain_decks=retain_decks,
            storage=request.app.state.storage,
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


@router.post("/{project_id}/materials/{material_id}/replace", status_code=200)
@router.post("/{project_id}/replace-pdf", status_code=200, include_in_schema=False)
async def replace_pdf_endpoint(
    request: Request,
    project_id: str,
    material_id: str,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """仅解析失败项目可替换并重新解析（原子替换 PDF）。"""
    user_id: str = request.state.principal.user_id
    key = get_idempotency_key(request)
    path = f"/projects/{project_id}/materials/{material_id}/replace"
    data, storage_key = await _upload(request, file)
    body_hash = request_body_hash(data)

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        body = replace_pdf(
            session,
            user_id=user_id,
            project_id=project_id,
            material_id=material_id,
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
