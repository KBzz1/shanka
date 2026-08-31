"""services.projects.service：学习项目用例（structure-contract 3.16/3.2a/6.2；V2.5 多资料）。

- 项目为聚合根 = 资料集合（V25-D-29）：materials 表承载归属（PDF 资料与 pdf_files 一对一，
  解析状态/存储以 pdf_files 为权威；TEXT 资料即时就绪、单章节+段落多 chunk）。
- status 由全部资料状态与 chapters_confirmed_at 聚合派生（3.16，不建第二套状态列）；
  新增/删除任一资料重置 chapters_confirmed_at（V25-D-31）。
- 删除语义（PRD V25-GEN-FR-09）：retain_decks=true 删除项目聚合，保留已发布牌组与卡片；
  retain_decks=false 删除整个聚合。资料级删除（V25-D-30）为独立三档语义（6.2）。
- 存储补偿：storage.delete 在 DB 删除之后、事务提交之前执行；失败抛错 → 调用方不 commit
  → 元数据整体回滚（绝不宣称成功却半删，可重试）。
- 删除安全：解析中的 PDF 使用版本栅栏，活跃任务在确认删除时由服务端自动取消；迟到的
  parser/worker 结果不得写回已删除项目。
- 章节删除（V25-GEN-FR-02）：被活跃任务引用的章节不可删；delete_cards 决定卡去留，
  保留的卡 chapter_id 置空进入"未归属章节"；章节同步移出新卡范围。
- 事务语义：本模块函数不 commit/rollback，由调用方（handler）控制。
"""

import json
import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from domain.card import VISIBLE_PREDICATE_SQL
from domain.task import ACTIVE_TASK_STATUSES as _ACTIVE_TASK_STATUSES
from infra.clock import SystemClock
from infra.db.models import (
    Card,
    Chapter,
    Deck,
    GenerationOperation,
    LearningProject,
    LlmCallAttempt,
    Material,
    PdfFile,
    ProjectStudyDeck,
    ProjectStudySettings,
    Task,
)
from infra.db.session import format_utc
from services.deletion.service import (
    cancel_active_tasks as cancel_active_generation_tasks,
)
from services.deletion.service import (
    preflight_payload,
    resource_tasks,
)
from services.pdf.service import chapter_view, delete_chapter, update_chapter, upload_pdf
from services.pdf.text_chunks import persist_text_material_chunks


def _uuid4() -> str:
    return str(uuid.uuid4())


def _validate_name(name: str) -> str:
    """去首尾空白后 1~60 字符（契约 3.16），可重名。"""
    stripped = name.strip()
    if not stripped or len(stripped) > 60:
        raise AppError(ErrorCode.VALIDATION_ERROR, "项目名须为去首尾空白后 1~60 字符")
    return stripped


def _validate_daily_goal(value: int, field: str) -> None:
    if value < 0 or value > 200 or value % 10 != 0:
        raise AppError(ErrorCode.VALIDATION_ERROR, f"{field} 须为 0~200 的 10 倍数")


def _owned_project(session: Session, *, user_id: str, project_id: str) -> LearningProject:
    """项目不存在或跨用户 → 404 PROJECT_NOT_FOUND（6.2 统一 404，不暴露存在性）。"""
    project = session.get(LearningProject, project_id)
    if project is None or project.user_id != user_id:
        raise AppError(ErrorCode.PROJECT_NOT_FOUND, "项目不存在或无权访问")
    return project


def _project_pdfs(session: Session, project_id: str) -> list[PdfFile]:
    """项目全部 PDF 资料行（materials×pdf_files 一对一，material_id == file_id）。"""
    return list(
        session.scalars(
            select(PdfFile)
            .join(Material, Material.material_id == PdfFile.file_id)
            .where(Material.project_id == project_id)
            .order_by(Material.created_at, Material.material_id)
        ).all()
    )


def _project_materials(session: Session, project_id: str) -> list[Material]:
    return list(
        session.scalars(
            select(Material)
            .where(Material.project_id == project_id)
            .order_by(Material.created_at, Material.material_id)
        ).all()
    )


def _pdf_status_map(session: Session, project_id: str) -> dict[str, str]:
    """{material_id: pdf_status}（仅 PDF 资料行）。"""
    rows = session.execute(
        select(Material.material_id, PdfFile.status)
        .join(PdfFile, PdfFile.file_id == Material.material_id)
        .where(Material.project_id == project_id)
    ).all()
    return {mid: status for mid, status in rows}


def _derive_status(
    session: Session, project: LearningProject, chapters_confirmed_at: str | None
) -> str:
    """项目状态聚合派生（3.16）：EMPTY/PARSING/PARSE_FAILED/AWAITING/READY。"""
    pdf_statuses = list(_pdf_status_map(session, project.project_id).values())
    if not pdf_statuses and not _project_materials(session, project.project_id):
        return "EMPTY"
    if any(status in ("PENDING", "PARSING") for status in pdf_statuses):
        return "PARSING"
    # 可用章节来源：任一 PARSED PDF 或任一 TEXT 资料
    has_source = (
        "PARSED" in pdf_statuses
        or _count(
            session, Material, Material.project_id == project.project_id, Material.type == "TEXT"
        )
        > 0
    )
    if not has_source:
        return "PARSE_FAILED"
    return "READY" if chapters_confirmed_at else "AWAITING_CHAPTER_CONFIRMATION"


def _reset_chapter_confirmation(project: LearningProject, *, now: str) -> None:
    """材质增删重置章节确认（V25-D-31）。"""
    project.chapters_confirmed_at = None
    project.updated_at = now
    project.version = now


def _count(session: Session, model: type, *whereclause: Any) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*whereclause)) or 0


def material_view(session: Session, material: Material) -> dict[str, Any]:
    """Material 视图（openapi Material，3.2a）：PDF 行状态取自 pdf_files（单一权威）。"""
    material_type = material.type
    pdf_status: str | None = None
    error_code = material.error_code
    size_bytes = material.size_bytes
    if material_type == "PDF":
        pdf = session.get(PdfFile, material.material_id)
        pdf_status = pdf.status if pdf is not None else "FAILED"
        error_code = pdf.error_code if pdf is not None else "PDF_PARSE_FAILED"
        size_bytes = pdf.size_bytes if pdf is not None else material.size_bytes
    chapter: dict[str, Any] | None = None
    if material_type == "TEXT":
        row = session.scalars(
            select(Chapter).where(Chapter.material_id == material.material_id)
        ).first()
        if row is not None:
            chapter = chapter_view(row)
    status = "READY" if material_type == "TEXT" else pdf_status
    return {
        "material_id": material.material_id,
        "project_id": material.project_id,
        "type": material_type,
        "name": material.name,
        "status": status,
        "error_code": error_code,
        "size_bytes": size_bytes,
        "char_count": material.char_count,
        "chapter": chapter,
        "created_at": material.created_at,
    }


def project_view(
    session: Session, project: LearningProject, *, with_chapters: bool = True
) -> dict[str, Any]:
    """项目视图（openapi LearningProject）：materials/chapters 摘要 + 派生计数 + 聚合状态。"""
    materials = _project_materials(session, project.project_id)
    material_ids = [m.material_id for m in materials]
    chapters = None
    if with_chapters and material_ids:
        rows = session.scalars(
            select(Chapter)
            .where(Chapter.material_id.in_(material_ids))
            .order_by(Chapter.start_page, Chapter.name)
        ).all()
        chapters = [chapter_view(ch) for ch in rows]
    view: dict[str, Any] = {
        "project_id": project.project_id,
        "name": project.name,
        "materials": [material_view(session, m) for m in materials],
        "status": _derive_status(session, project, project.chapters_confirmed_at),
        "chapter_count": _count(session, Chapter, Chapter.material_id.in_(material_ids))
        if material_ids
        else 0,
        "deck_count": _count(session, Deck, Deck.project_id == project.project_id),
        "task_count": _count(session, Task, Task.project_id == project.project_id),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "version": project.version,
    }
    if with_chapters and chapters is not None:
        view["chapters"] = chapters
    # Lists stay lightweight; detail includes the persisted task snapshot so a restarted app can
    # render every draft/sample/generation state without relying on an in-memory _pdfTask.
    if with_chapters:
        from services.tasks.service import task_view

        tasks = session.scalars(
            select(Task)
            .where(Task.project_id == project.project_id, Task.user_id == project.user_id)
            .order_by(Task.created_at.desc(), Task.task_id.desc())
        ).all()
        view["tasks"] = [task_view(task) for task in tasks]
    return view


def create_project(session: Session, *, user_id: str, name: str, now: str) -> dict[str, Any]:
    """两步创建第一步（POST /projects，契约 6.2）：仅名称的空项目（V25-D-29）。"""
    final_name = _validate_name(name)
    project = LearningProject(
        project_id=_uuid4(),
        user_id=user_id,
        name=final_name,
        chapters_confirmed_at=None,
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(project)
    session.flush()
    # A newly created project starts with an explicitly unconfigured deck-scoped plan.  Keeping
    # the row lets today's queue distinguish a fresh project from pre-plan legacy rows that are
    # still served by the compatibility chapter scope.
    session.add(
        ProjectStudySettings(
            project_id=project.project_id,
            selected_chapter_ids="[]",
            include_unassigned=0,
            daily_new_goal=10,
            daily_review_goal=40,
            updated_at=now,
        )
    )
    session.flush()
    return project_view(session, project)


def add_pdf_material(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    filename: str,
    size_bytes: int,
    storage_key: str,
    now: str,
) -> dict[str, Any]:
    """添加 PDF 资料（POST /materials/pdf）：登记 pdf_files + materials 并重置确认（V25-D-31）。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    pdf = upload_pdf(
        session,
        user_id=user_id,
        filename=filename,
        size_bytes=size_bytes,
        storage_key=storage_key,
        now=now,
    )
    session.flush()  # 先落 pdf_files 行（UoW 顺序）
    material = Material(
        material_id=pdf.file_id,
        project_id=project.project_id,
        type="PDF",
        name=filename,
        status=None,  # 解析状态以 pdf_files 为权威
        size_bytes=size_bytes,
        created_at=now,
    )
    session.add(material)
    _reset_chapter_confirmation(project, now=now)
    session.flush()
    return material_view(session, material)


def add_text_material(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    name: str,
    content: str,
    now: str,
    settings: Settings,
) -> dict[str, Any]:
    """添加粘贴文本资料（POST /materials/text，V25-D-32）：单章节 + 段落多 chunk，即时就绪。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    title = _validate_name(name)
    stripped = content.strip()
    if not stripped or len(stripped) > 30000:
        raise AppError(ErrorCode.VALIDATION_ERROR, "文本资料须为 1~30000 字")
    material = Material(
        material_id=_uuid4(),
        project_id=project.project_id,
        type="TEXT",
        name=title,
        status="READY",
        char_count=len(stripped),
        created_at=now,
    )
    session.add(material)
    session.flush()
    session.add(
        Chapter(
            chapter_id=_uuid4(),
            file_id=None,
            material_id=material.material_id,
            name=title,
            start_page=None,
            end_page=None,
        )
    )
    session.flush()
    persist_text_material_chunks(
        session,
        material_id=material.material_id,
        content=stripped,
        target_chars=settings.text_chunk_target_chars,
        now=now,
    )
    _reset_chapter_confirmation(project, now=now)
    session.flush()
    return material_view(session, material)


def list_materials(session: Session, *, user_id: str, project_id: str) -> list[dict[str, Any]]:
    _owned_project(session, user_id=user_id, project_id=project_id)
    return [material_view(session, m) for m in _project_materials(session, project_id)]


def delete_material(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    material_id: str,
    retain_cards: bool,
    storage: Any,
    now: str,
) -> dict[str, Any]:
    """资料级删除（DELETE /materials/{material_id}，V25-D-30 三档语义）。

    - 静默取消引用该资料的活跃任务（CAS + fencing，不向用户暴露任务选项）；
    - retain_cards=true 保留该资料产出卡片（chapter_id 置空脱离）；false 连同删除；
    - 章节与 chunk 经 FK 级联；PDF 资料连带删 pdf_files 行与存储对象；
    - 删最后一份资料后项目保留为空项目；均重置章节确认（V25-D-31）。
    """
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    material = session.get(Material, material_id)
    if material is None or material.project_id != project_id:
        raise AppError(ErrorCode.MATERIAL_NOT_FOUND, "资料不存在")
    pdf = session.get(PdfFile, material.material_id) if material.type == "PDF" else None
    active_tasks = resource_tasks(session, user_id=user_id, project_id=project_id)
    # 仅取消快照引用本资料的活跃任务（V25-D-30：静默取消，不向用户暴露任务选项）
    referencing = [
        task for task in active_tasks if _snapshot_references_material(task, material_id)
    ]
    if referencing:
        cancel_active_generation_tasks(
            session,
            user_id=user_id,
            tasks=referencing,
            now=now,
            resource_type="PROJECT",
            resource_id=project_id,
        )
    if not retain_cards:
        # 该资料产出的卡片：经章节集合定位，连同复习数据级联删除（用户明确选择）
        chapter_ids = list(
            session.scalars(
                select(Chapter.chapter_id).where(Chapter.material_id == material_id)
            ).all()
        )
        if chapter_ids:
            for card in session.scalars(
                select(Card).where(Card.user_id == user_id, Card.chapter_id.in_(chapter_ids))
            ).all():
                session.delete(card)
            session.flush()
        _remove_chapters_from_settings(session, project_id, chapter_ids)
    # 先删资料行（chapters/text_chunks 级联），再删 PDF 行与存储
    session.delete(material)
    session.flush()
    if pdf is not None:
        if pdf.status in ("PENDING", "PARSING"):
            pdf.parse_version += 1
            pdf.parse_lease_token = None
            pdf.parse_lease_until = None
        session.delete(pdf)
        session.flush()
        storage.delete(pdf.storage_key)
    _reset_chapter_confirmation(project, now=now)
    session.flush()
    return project_view(session, project)


def _snapshot_items(task: Task) -> list[dict[str, Any]]:
    try:
        snapshot = json.loads(task.selected_chapters)
    except (ValueError, TypeError):
        return []
    return (
        [item for item in snapshot if isinstance(item, dict)] if isinstance(snapshot, list) else []
    )


def _snapshot_references_material(task: Task, material_id: str) -> bool:
    return any(item.get("material_id") == material_id for item in _snapshot_items(task))


def list_projects(session: Session, *, user_id: str) -> list[dict[str, Any]]:
    """项目列表（含空项目；V25-D-29 资料集合语义下不再要求 1:1 PDF 行存在）。"""
    projects = session.scalars(
        select(LearningProject)
        .where(LearningProject.user_id == user_id)
        .order_by(LearningProject.updated_at.desc())
    ).all()
    return [project_view(session, p, with_chapters=False) for p in projects]


def get_project(session: Session, *, user_id: str, project_id: str) -> dict[str, Any]:
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    return project_view(session, project)


def rename_project(
    session: Session, *, user_id: str, project_id: str, name: str, now: str
) -> dict[str, Any]:
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    project.name = _validate_name(name)
    project.updated_at = now
    project.version = now  # 缓存刷新与并发检查：任何项目行写入刷新
    return project_view(session, project)


def confirm_chapters(
    session: Session, *, user_id: str, project_id: str, now: str
) -> dict[str, Any]:
    """确认目录 → READY（3.16）；无可确认来源或已确认 → 409 PROJECT_STATE_CONFLICT。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    if _derive_status(session, project, project.chapters_confirmed_at) in (
        "AWAITING_CHAPTER_CONFIRMATION",
        "READY",
    ):
        if project.chapters_confirmed_at is not None:
            raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "章节已确认（项目已处于可制卡状态）")
    else:
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "尚无已就绪资料，无法确认章节")
    project.chapters_confirmed_at = now
    project.updated_at = now
    project.version = now
    return project_view(session, project)


def replace_pdf(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    material_id: str,
    filename: str,
    size_bytes: int,
    storage_key: str,
    now: str,
    storage: Any,
) -> dict[str, Any]:
    """仅 FAILED 的 PDF 资料可原位替换（V25-GEN-FR-01/契约 6.2；不影响其他资料）。

    顺序：新 PDF 行 + 资料改指先 flush（防 UoW 乱序 FK 违约），再删旧 PDF 行
    （chapters/text_chunks 级联），最后清理旧存储对象——清理失败抛错 → 调用方回滚。
    """
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    material = session.get(Material, material_id)
    if material is None or material.project_id != project_id or material.type != "PDF":
        raise AppError(ErrorCode.MATERIAL_NOT_FOUND, "PDF 资料不存在")
    old_pdf = session.get(PdfFile, material.material_id)
    if old_pdf is None or old_pdf.status != "FAILED":
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "仅解析失败的 PDF 资料可替换")
    new_pdf = upload_pdf(
        session,
        user_id=user_id,
        filename=filename,
        size_bytes=size_bytes,
        storage_key=storage_key,
        now=now,
    )
    session.flush()  # 先落新 PDF 行（显式顺序，防 UoW 乱序导致 FK 违约）
    # 资料行整体替换（不改主键）：删旧行（chapters/text_chunks 级联）→ 建新行
    session.delete(material)
    session.flush()
    session.add(
        Material(
            material_id=new_pdf.file_id,
            project_id=project.project_id,
            type="PDF",
            name=filename,
            status=None,
            size_bytes=size_bytes,
            created_at=now,
        )
    )
    _reset_chapter_confirmation(project, now=now)
    session.flush()
    session.delete(old_pdf)
    session.flush()
    storage.delete(old_pdf.storage_key)
    return project_view(session, project)


def delete_project(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    retain_decks: bool,
    storage: Any,
    now: str | None = None,
) -> None:
    """删除项目聚合（两决策，PRD V25-GEN-FR-09）。存储清理失败 → 抛错回滚元数据。

    顺序（显式 flush 防 UoW 无 relationship 乱序）：项目牌组（可选）→ 任务历史（KP/批次
    级联）→ 项目设置 → 项目行（user_preferences.current_project_id 与 decks.project_id
    FK SET NULL）→ PDF 行（chapters/text_chunks 级联、cards.chapter_id SET NULL）→
    存储对象；存储删除失败抛错，调用方不 commit → 全部回滚（绝不宣称成功却半删）。
    """
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    # PDF 解析采用租约 + 版本栅栏；删除可以在 PENDING/PARSING 时进行，迟到的解析结果
    # 会因 PDF 行/parse_version 不匹配而丢弃，不能再阻塞用户清理项目。
    project_pdfs = _project_pdfs(session, project_id)
    for pdf in project_pdfs:
        if pdf.status in ("PENDING", "PARSING"):
            pdf.parse_version += 1
            pdf.parse_lease_token = None
            pdf.parse_lease_until = None
    active_tasks = resource_tasks(session, user_id=user_id, project_id=project_id)
    if active_tasks:
        # 产品语义（契约 570/675）：确认删除后全部关联活跃任务在同一写事务内 CAS 取消。
        cancel_active_generation_tasks(
            session,
            user_id=user_id,
            tasks=active_tasks,
            now=now or format_utc(SystemClock().now_utc()),
            resource_type="PROJECT",
            resource_id=project_id,
        )
    if not retain_decks:
        # 牌组删除 → 卡片 → review_states/review_events 经 FK CASCADE 级联（PRAGMA ON）
        for deck in session.scalars(select(Deck).where(Deck.project_id == project_id)).all():
            session.delete(deck)
    # 任务历史全删：knowledge_points/batches/llm_call_attempts 级联；
    # 保留牌组的已发布卡 source_task_id 经 FK SET NULL（来源查看不可再承诺）
    project_tasks = list(session.scalars(select(Task).where(Task.project_id == project_id)).all())
    _detach_task_history(session, project_tasks, now=now or format_utc(SystemClock().now_utc()))
    for task in project_tasks:
        session.delete(task)
    settings = session.get(ProjectStudySettings, project_id)
    if settings is not None:
        session.delete(settings)
    session.delete(project)
    session.flush()  # 先删项目行（materials.project_id 级联删资料；TEXT 随行清理）
    for pdf in project_pdfs:
        session.delete(pdf)
    session.flush()
    for pdf in project_pdfs:
        storage.delete(pdf.storage_key)


def _active_task_count(session: Session, project_id: str) -> int:
    return (
        session.scalar(
            select(func.count(Task.task_id)).where(
                Task.project_id == project_id,
                Task.status.in_(_ACTIVE_TASK_STATUSES),
            )
        )
        or 0
    )


def project_deletion_preflight(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    retain_decks: bool,
    allow_cancel: bool = False,
) -> dict[str, object]:
    """Read-only deletion preview; the DELETE endpoint repeats all checks atomically."""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    decks = session.scalars(select(Deck).where(Deck.project_id == project_id)).all()
    deck_ids = [deck.deck_id for deck in decks]
    card_count = (
        _count(
            session,
            Card,
            Card.user_id == user_id,
            Card.deck_id.in_(deck_ids),
        )
        if deck_ids and not retain_decks
        else 0
    )
    blockers = resource_tasks(session, user_id=user_id, project_id=project_id)
    payload = preflight_payload(
        session,
        user_id=user_id,
        resource_type="PROJECT",
        resource_id=project_id,
        project_id=project_id,
        allow_cancel=allow_cancel,
        impact={
            "retain_decks": retain_decks,
            "deck_count": len(decks),
            "card_count": card_count,
            "task_count": _count(session, Task, Task.project_id == project_id),
            "project_status": _derive_status(session, project, project.chapters_confirmed_at),
        },
    )
    payload["project_state_blocked"] = False
    # Keep this explicit local read so a future implementation cannot accidentally compute
    # `can_delete` from a second, differently-filtered query.
    payload["blocking_task_count"] = len(blockers)
    return payload


def _detach_task_history(session: Session, tasks: list[Task], *, now: str) -> None:
    """保留 LLM 对账行但解除已删除任务的外键/快照引用。"""
    for task in tasks:
        session.execute(
            update(LlmCallAttempt)
            .where(LlmCallAttempt.task_id == task.task_id)
            .values(task_id=None)
        )
        operations = session.scalars(
            select(GenerationOperation).where(GenerationOperation.task_id == task.task_id)
        ).all()
        for operation in operations:
            if operation.status == "ACTIVE":
                operation.status = "ABANDONED"
                operation.ended_at = now
                operation.terminal_reason = "RESOURCE_DELETE"
            operation.task_id = None
            operation.updated_at = now


def update_project_chapter(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    chapter_id: str,
    name: str | None,
    start_page: int | None,
    end_page: int | None,
    now: str,
) -> Chapter:
    """项目路由修改章节：委托 pdf 服务同一章节实现；状态冲突换项目语义错误码。"""
    _owned_project(session, user_id=user_id, project_id=project_id)  # 归属校验（404）
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or not _chapter_in_project(session, project_id, chapter_id):
        raise AppError(ErrorCode.CHAPTER_NOT_FOUND, "章节不存在")
    return update_chapter(
        session,
        user_id=user_id,
        file_id=chapter.file_id or chapter.material_id,
        chapter_id=chapter_id,
        name=name,
        start_page=start_page,
        end_page=end_page,
        now=now,
        conflict_error=ErrorCode.PROJECT_STATE_CONFLICT,
    )


def delete_project_chapter(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    chapter_id: str,
    delete_cards: bool,
) -> None:
    """删除章节（V25-GEN-FR-02）：活跃任务保护；delete_cards 决定卡去留，保留的卡
    chapter_id 置空进入"未归属章节"；章节同步移出新卡范围；KP chapter_id 置 null。"""
    _owned_project(session, user_id=user_id, project_id=project_id)  # 归属校验（404）
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or not _chapter_in_project(session, project_id, chapter_id):
        raise AppError(ErrorCode.CHAPTER_NOT_FOUND, "章节不存在")
    if _chapter_referenced_by_active_task(session, project_id, chapter_id):
        raise AppError(
            ErrorCode.PROJECT_HAS_ACTIVE_TASK,
            "章节被进行中的制卡任务引用，请先等待完成或放弃",
        )
    if delete_cards:
        # 该章节生成的卡片连同复习数据级联删除（用户明确选择）；先删卡（显式顺序，
        # 卡无 chapter_id ondelete，避免章节先删导致 SET NULL 后卡失去删除依据）
        for card in session.scalars(
            select(Card).where(Card.chapter_id == chapter_id, Card.user_id == user_id)
        ).all():
            session.delete(card)
        session.flush()
    _remove_chapter_from_settings(session, project_id, chapter_id)
    delete_chapter(
        session,
        user_id=user_id,
        file_id=chapter.file_id or chapter.material_id,
        chapter_id=chapter_id,
    )


def _chapter_referenced_by_active_task(session: Session, project_id: str, chapter_id: str) -> bool:
    """被草稿/样卡生成中/待确认样卡/生成中任务引用（selected_chapters 快照含章节）。"""
    tasks = session.scalars(
        select(Task).where(
            Task.project_id == project_id,
            Task.status.in_(_ACTIVE_TASK_STATUSES),
        )
    ).all()
    return any(_snapshot_has_chapter(task, chapter_id) for task in tasks)


def _snapshot_has_chapter(task: Task, chapter_id: str) -> bool:
    try:
        snapshot = json.loads(task.selected_chapters)
    except (ValueError, TypeError):
        return False
    return any(isinstance(item, dict) and item.get("chapter_id") == chapter_id for item in snapshot)


def _remove_chapters_from_settings(
    session: Session, project_id: str, chapter_ids: list[str]
) -> None:
    """多章节批量移出新卡范围（资料级删除；逐章调用单一移除实现）。"""
    for chapter_id in chapter_ids:
        _remove_chapter_from_settings(session, project_id, chapter_id)


def _remove_chapter_from_settings(session: Session, project_id: str, chapter_id: str) -> None:
    """章节移出新卡范围（3.17；PRD：删除章节将退出后续新卡范围）。"""
    row = session.get(ProjectStudySettings, project_id)
    if row is None:
        return
    try:
        ids = json.loads(row.selected_chapter_ids)
    except (ValueError, TypeError):
        ids = []
    if chapter_id in ids:
        row.selected_chapter_ids = json.dumps(
            [i for i in ids if i != chapter_id], ensure_ascii=False
        )


def get_study_settings(
    session: Session, *, user_id: str, project_id: str, now: str
) -> dict[str, Any]:
    """项目学习设置（3.17）：get-or-create（默认空范围 + include_unassigned=false）。"""
    _owned_project(session, user_id=user_id, project_id=project_id)  # 归属校验（404）
    return _settings_view(session, _get_or_create_settings(session, project_id=project_id, now=now))


def update_study_settings(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    payload: dict[str, Any],
    now: str,
) -> dict[str, Any]:
    """部分更新（last-success-wins）：范围章节须属于本项目（404 CHAPTER_NOT_FOUND）；
    空部分更新 = 真 no-op（不刷新 updated_at，与 preferences 同款语义）。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    row = _get_or_create_settings(session, project_id=project_id, now=now)
    updates: dict[str, Any] = {}
    if payload.get("selected_new_card_chapter_ids") is not None:
        ids = payload["selected_new_card_chapter_ids"]
        _validate_chapter_ids(session, project, ids)
        updates["selected_chapter_ids"] = json.dumps(ids, ensure_ascii=False)
    if payload.get("include_unassigned") is not None:
        updates["include_unassigned"] = 1 if payload["include_unassigned"] else 0
    selected_deck_ids = payload.get("selected_deck_ids")
    if selected_deck_ids is not None:
        unique_deck_ids = list(dict.fromkeys(selected_deck_ids))
        deck_query = select(Deck).where(Deck.user_id == user_id, Deck.project_id == project_id)
        if unique_deck_ids:
            deck_query = deck_query.where(Deck.deck_id.in_(unique_deck_ids))
        decks = list(session.scalars(deck_query).all())
        if {deck.deck_id for deck in decks} != set(unique_deck_ids):
            raise AppError(ErrorCode.DECK_NOT_FOUND, "所选卡组不存在或不属于当前项目")
        if unique_deck_ids:
            eligible = _count(
                session,
                Card,
                Card.user_id == user_id,
                Card.deck_id.in_(unique_deck_ids),
                text(VISIBLE_PREDICATE_SQL),
            )
            if eligible == 0:
                raise AppError(ErrorCode.VALIDATION_ERROR, "所选卡组暂无可学习卡片")
        session.query(ProjectStudyDeck).filter(ProjectStudyDeck.project_id == project_id).delete(
            synchronize_session=False
        )
        for deck_id in unique_deck_ids:
            session.add(ProjectStudyDeck(project_id=project_id, deck_id=deck_id, created_at=now))
        updates["daily_new_goal"] = row.daily_new_goal
        updates["daily_review_goal"] = row.daily_review_goal
    if payload.get("daily_new_goal") is not None:
        _validate_daily_goal(payload["daily_new_goal"], "每日新学目标")
        updates["daily_new_goal"] = payload["daily_new_goal"]
    if payload.get("daily_review_goal") is not None:
        _validate_daily_goal(payload["daily_review_goal"], "每日巩固目标")
        updates["daily_review_goal"] = payload["daily_review_goal"]
    if (
        updates.get("daily_new_goal", row.daily_new_goal)
        + updates.get("daily_review_goal", row.daily_review_goal)
        == 0
    ):
        raise AppError(ErrorCode.VALIDATION_ERROR, "每日新学和巩固目标不能同时为 0")
    if not updates:
        return _settings_view(session, row)
    updates["updated_at"] = now
    for column, value in updates.items():
        setattr(row, column, value)
    return _settings_view(session, row)


def _chapter_in_project(session: Session, project_id: str, chapter_id: str) -> bool:
    """章节是否属于项目任一资料（多资料语义；V25-D-29）。"""
    row = session.execute(
        select(Chapter.chapter_id)
        .join(Material, Material.material_id == Chapter.material_id)
        .where(Chapter.chapter_id == chapter_id, Material.project_id == project_id)
    ).first()
    return row is not None


def _validate_chapter_ids(session: Session, project: LearningProject, ids: list[str]) -> None:
    """范围章节必须属于项目任一资料（不存在/他属 → 404 CHAPTER_NOT_FOUND）。"""
    if not ids:
        return
    found = {cid for cid in ids if _chapter_in_project(session, project.project_id, cid)}
    if found != set(ids):
        raise AppError(ErrorCode.CHAPTER_NOT_FOUND, "章节不存在或不属于该项目")


def _get_or_create_settings(session: Session, *, project_id: str, now: str) -> ProjectStudySettings:
    row = session.get(ProjectStudySettings, project_id)
    if row is not None:
        return row
    row = ProjectStudySettings(
        project_id=project_id,
        selected_chapter_ids="[]",
        include_unassigned=0,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _settings_view(session: Session, row: ProjectStudySettings) -> dict[str, Any]:
    try:
        ids = json.loads(row.selected_chapter_ids)
    except (ValueError, TypeError):
        ids = []
    return {
        "selected_new_card_chapter_ids": ids,
        "include_unassigned": bool(row.include_unassigned),
        "selected_deck_ids": list(
            session.scalars(
                select(ProjectStudyDeck.deck_id)
                .where(ProjectStudyDeck.project_id == row.project_id)
                .order_by(ProjectStudyDeck.created_at, ProjectStudyDeck.deck_id)
            ).all()
        ),
        "daily_new_goal": int(row.daily_new_goal),
        "daily_review_goal": int(row.daily_review_goal),
        "updated_at": row.updated_at,
    }
