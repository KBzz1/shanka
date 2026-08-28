"""services.projects.service：学习项目用例（structure-contract 3.16/3.17/6.2；V2.5 新增）。

- 项目为聚合根：一个项目恰好一份当前 PDF（learning_projects.file_id 唯一外键权威）；
  status 由 PDF 状态与 chapters_confirmed_at 派生（3.16，不建可漂移的第二套状态列）。
- 删除语义（PRD V25-GEN-FR-09，两种用户决策）：retain_decks=true 删除项目聚合
  （PDF/章节/任务历史/项目设置），保留已发布牌组与卡片（cards.chapter_id 置空脱离项目）；
  retain_decks=false 删除整个聚合（牌组/卡片/复习数据经 FK 级联）。
- 存储补偿：storage.delete 在 DB 删除之后、事务提交之前执行；失败抛错 → 调用方不 commit
  → 元数据整体回滚（绝不宣称成功却半删，可重试）。
- 删除保护：解析中项目 → PROJECT_STATE_CONFLICT；活跃（非终态）任务 → PROJECT_HAS_ACTIVE_TASK。
- 章节删除（V25-GEN-FR-02）：被活跃任务引用的章节不可删；delete_cards 决定卡去留，
  保留的卡 chapter_id 置空进入"未归属章节"；章节同步移出新卡范围。
- 兼容 /pdfs 路由委托本服务同一业务模型（6.2 注，无第二套项目/任务状态）。
- 事务语义：本模块函数不 commit/rollback，由调用方（handler）控制。
"""

import json
import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.task import ACTIVE_TASK_STATUSES as _ACTIVE_TASK_STATUSES
from infra.clock import SystemClock
from infra.db.models import (
    Card,
    Chapter,
    Deck,
    GenerationOperation,
    LearningProject,
    LlmCallAttempt,
    PdfFile,
    ProjectStudySettings,
    Task,
)
from infra.db.session import format_utc
from services.deletion.service import (
    abandon_pre_generation,
    preflight_payload,
    resource_tasks,
)
from services.deletion.service import (
    cancel_active_tasks as cancel_active_generation_tasks,
)
from services.pdf.service import chapter_view, delete_chapter, pdf_view, update_chapter, upload_pdf


def _uuid4() -> str:
    return str(uuid.uuid4())


def _validate_name(name: str) -> str:
    """去首尾空白后 1~60 字符（契约 3.16），可重名。"""
    stripped = name.strip()
    if not stripped or len(stripped) > 60:
        raise AppError(ErrorCode.VALIDATION_ERROR, "项目名须为去首尾空白后 1~60 字符")
    return stripped


def _default_name(filename: str) -> str:
    """缺省项目名：上传文件名去扩展名（与 V2.5 迁移回填同口径）；超长截断、空兜底。"""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = stem.strip() or "未命名项目"
    return stem[:60]


def _owned_project(session: Session, *, user_id: str, project_id: str) -> LearningProject:
    """项目不存在或跨用户 → 404 PROJECT_NOT_FOUND（6.2 统一 404，不暴露存在性）。"""
    project = session.get(LearningProject, project_id)
    if project is None or project.user_id != user_id:
        raise AppError(ErrorCode.PROJECT_NOT_FOUND, "项目不存在或无权访问")
    return project


def _require_pdf(session: Session, project: LearningProject) -> PdfFile:
    """项目必有当前 PDF（file_id 非空唯一外键）；行缺失 = 数据完整性异常，不泛化 404。"""
    pdf = session.get(PdfFile, project.file_id)
    if pdf is None:
        raise AppError(ErrorCode.INTERNAL_ERROR, "项目缺少当前 PDF 记录")
    return pdf


def _project_status(pdf: PdfFile, chapters_confirmed_at: str | None) -> str:
    """项目状态派生（3.16）：PARSING/PARSE_FAILED/AWAITING_CHAPTER_CONFIRMATION/READY。"""
    if pdf.status == "PARSED":
        return "READY" if chapters_confirmed_at else "AWAITING_CHAPTER_CONFIRMATION"
    if pdf.status == "FAILED":
        return "PARSE_FAILED"
    return "PARSING"  # PENDING/PARSING


def _count(session: Session, model: type, *whereclause: Any) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*whereclause)) or 0


def project_view(
    session: Session, project: LearningProject, *, with_chapters: bool = True
) -> dict[str, Any]:
    """项目视图（openapi LearningProject）：file/chapters 摘要 + 派生计数 + 派生状态。"""
    pdf = _require_pdf(session, project)
    chapters = None
    if with_chapters and pdf.status == "PARSED":
        rows = session.scalars(
            select(Chapter).where(Chapter.file_id == pdf.file_id).order_by(Chapter.start_page)
        ).all()
        chapters = [chapter_view(ch) for ch in rows]
    view: dict[str, Any] = {
        "project_id": project.project_id,
        "name": project.name,
        "file": pdf_view(pdf, chapters),
        "status": _project_status(pdf, project.chapters_confirmed_at),
        "chapter_count": _count(session, Chapter, Chapter.file_id == pdf.file_id),
        "deck_count": _count(session, Deck, Deck.project_id == project.project_id),
        "task_count": _count(session, Task, Task.project_id == project.project_id),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "version": project.version,
    }
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


def create_project(
    session: Session,
    *,
    user_id: str,
    filename: str,
    size_bytes: int,
    storage_key: str,
    now: str,
    name: str | None = None,
) -> dict[str, Any]:
    """上传 PDF 建立学习项目（POST /projects 与兼容 POST /pdfs 共用同一业务模型）。

    上传成功即建立项目（V25-GEN-FR-01），PDF 异步解析由扫描器接管（本项目不落解析逻辑）。
    """
    final_name = _validate_name(name) if name is not None else _default_name(filename)
    pdf = upload_pdf(
        session,
        user_id=user_id,
        filename=filename,
        size_bytes=size_bytes,
        storage_key=storage_key,
        now=now,
    )
    session.flush()  # 先落 pdf_files 行（无 relationship 时 UoW 不保证插入顺序——既有模式）
    project = LearningProject(
        project_id=_uuid4(),
        user_id=user_id,
        file_id=pdf.file_id,
        name=final_name,
        chapters_confirmed_at=None,
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(project)
    session.flush()
    return project_view(session, project)


def list_projects(session: Session, *, user_id: str) -> list[dict[str, Any]]:
    # Historical SQLite files can contain pre-V2.5 project shells without a
    # current PDF row.  They cannot satisfy the one-project/one-PDF response
    # contract, so omit them from the list instead of making every valid project
    # unreadable with an INTERNAL_ERROR.  The rows and any learning data remain
    # untouched for explicit recovery.
    projects = session.scalars(
        select(LearningProject)
        .join(PdfFile, PdfFile.file_id == LearningProject.file_id)
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
    """确认目录 → READY（3.16）；未解析或已确认 → 409 PROJECT_STATE_CONFLICT。"""
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    pdf = _require_pdf(session, project)
    if pdf.status != "PARSED":
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "PDF 尚未解析完成，无法确认章节")
    if project.chapters_confirmed_at is not None:
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "章节已确认（项目已处于可制卡状态）")
    project.chapters_confirmed_at = now
    project.updated_at = now
    project.version = now
    return project_view(session, project)


def replace_pdf(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    filename: str,
    size_bytes: int,
    storage_key: str,
    now: str,
    storage: Any,
) -> dict[str, Any]:
    """仅解析失败项目可替换（原子替换当前 PDF 并重新解析；V25-GEN-FR-01）。

    顺序：新 PDF 行 + 项目改指先 flush（防 UoW 无 relationship 乱序触发 FK 违约），
    再删旧 PDF 行（chapters/text_chunks 级联、tasks.file_id SET NULL），最后清理旧存储
    对象——清理失败抛错 → 调用方回滚（元数据不半提交）。
    """
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    old_pdf = _require_pdf(session, project)
    if old_pdf.status != "FAILED":
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "仅解析失败的项目可替换 PDF")
    new_pdf = upload_pdf(
        session,
        user_id=user_id,
        filename=filename,
        size_bytes=size_bytes,
        storage_key=storage_key,
        now=now,
    )
    session.flush()  # 先落新 PDF 行（显式顺序，防 UoW 乱序导致 FK 违约）
    project.file_id = new_pdf.file_id
    project.chapters_confirmed_at = None  # 重新解析后需重新确认章节
    project.updated_at = now
    project.version = now
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
    abandon_pre_generation_tasks: bool = False,
    cancel_active_tasks: bool = False,
    now: str | None = None,
) -> None:
    """删除项目聚合（两决策，PRD V25-GEN-FR-09）。存储清理失败 → 抛错回滚元数据。

    顺序（显式 flush 防 UoW 无 relationship 乱序）：项目牌组（可选）→ 任务历史（KP/批次
    级联）→ 项目设置 → 项目行（user_preferences.current_project_id 与 decks.project_id
    FK SET NULL）→ PDF 行（chapters/text_chunks 级联、cards.chapter_id SET NULL）→
    存储对象；存储删除失败抛错，调用方不 commit → 全部回滚（绝不宣称成功却半删）。
    """
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    pdf = _require_pdf(session, project)
    if pdf.status in ("PENDING", "PARSING"):
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "项目解析中，暂时无法删除")
    active_tasks = resource_tasks(session, user_id=user_id, project_id=project_id)
    if active_tasks:
        if cancel_active_tasks:
            cancel_active_generation_tasks(
                session,
                user_id=user_id,
                tasks=active_tasks,
                now=now or format_utc(SystemClock().now_utc()),
                resource_type="PROJECT",
                resource_id=project_id,
            )
        elif abandon_pre_generation_tasks:
            abandon_pre_generation(
                session,
                user_id=user_id,
                tasks=active_tasks,
                now=now or format_utc(SystemClock().now_utc()),
                resource_type="PROJECT",
                resource_id=project_id,
            )
        else:
            can_abandon = [task.task_id for task in active_tasks if task.status != "GENERATING"]
            actions = (
                ("ABANDON_AND_RETRY", "VIEW_TASKS")
                if can_abandon
                else ("WAIT_FOR_TERMINAL", "VIEW_TASKS")
            )
            raise AppError(
                ErrorCode.PROJECT_HAS_ACTIVE_TASK,
                "存在进行中的制卡任务，请先放弃可放弃任务或等待正式生成完成",
                actions=actions,
                details={
                    "resource_type": "PROJECT",
                    "resource_id": project_id,
                    "task_ids": [task.task_id for task in active_tasks],
                    "abandonable_task_ids": can_abandon,
                },
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
    session.flush()  # 先删项目行（释放 learning_projects.file_id 引用）
    session.delete(pdf)
    session.flush()
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
    pdf = _require_pdf(session, project)
    decks = session.scalars(select(Deck).where(Deck.project_id == project_id)).all()
    deck_ids = [deck.deck_id for deck in decks]
    card_count = (
        _count(session, Card, Card.deck_id.in_(deck_ids)) if deck_ids and not retain_decks else 0
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
            "project_status": _project_status(pdf, project.chapters_confirmed_at),
        },
    )
    if pdf.status in ("PENDING", "PARSING"):
        payload["can_delete"] = False
        payload["actions"] = ["WAIT_FOR_TERMINAL", "VIEW_TASKS"]
        payload["project_state_blocked"] = True
    else:
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
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    return update_chapter(
        session,
        user_id=user_id,
        file_id=project.file_id,
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
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    pdf = _require_pdf(session, project)
    if pdf.status != "PARSED":
        raise AppError(ErrorCode.PROJECT_STATE_CONFLICT, "PDF 尚未解析完成，无法删除章节")
    chapter = session.get(Chapter, chapter_id)
    if chapter is None or chapter.file_id != project.file_id:
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
    delete_chapter(session, user_id=user_id, file_id=project.file_id, chapter_id=chapter_id)


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
    return _settings_view(_get_or_create_settings(session, project_id=project_id, now=now))


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
    if not updates:
        return _settings_view(row)
    updates["updated_at"] = now
    for column, value in updates.items():
        setattr(row, column, value)
    return _settings_view(row)


def _validate_chapter_ids(session: Session, project: LearningProject, ids: list[str]) -> None:
    """范围章节必须属于项目当前 PDF（不存在/他属 → 404 CHAPTER_NOT_FOUND）。"""
    if not ids:
        return
    found = set(
        session.scalars(
            select(Chapter.chapter_id).where(
                Chapter.file_id == project.file_id,
                Chapter.chapter_id.in_(ids),
            )
        ).all()
    )
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


def _settings_view(row: ProjectStudySettings) -> dict[str, Any]:
    try:
        ids = json.loads(row.selected_chapter_ids)
    except (ValueError, TypeError):
        ids = []
    return {
        "selected_new_card_chapter_ids": ids,
        "include_unassigned": bool(row.include_unassigned),
        "updated_at": row.updated_at,
    }
