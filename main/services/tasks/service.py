"""tasks/service.py：任务用例（创建/列表/修改/样卡请求/start/abandon/retry/删除 + 七态状态机）。

V2.5（structure-contract 4.1/6.4）：七态 DRAFT / SAMPLE_GENERATING /
AWAITING_SAMPLE_CONFIRMATION / GENERATING / COMPLETED / FAILED / ABANDONED。
用户侧无 PAUSED/resume/cancel——执行器内部经租约/心跳恢复，不暴露用户状态
（resume_task/cancel_task 已删除；abandon 取代取消语义）。

状态门卫集中在本模块（各操作前置状态表 _STATE_GATES）：
- 请求样卡：仅 DRAFT → SAMPLE_GENERATING；样卡由执行器 worker 后台完成
  （complete_samples 条件更新持久化，abandon 并发不复活）。
- 配置变更（PATCH）：仅 DRAFT/AWAITING_SAMPLE_CONFIRMATION，修改后样卡失效
  （sample_cards/sample_config_hash/sample_confirmed_at 清空 → DRAFT）。
- start：仅 AWAITING_SAMPLE_CONFIRMATION，校验 sample_config_hash（失配 →
  409 SAMPLE_STALE）→ GENERATING + internal_stage=PLANNING。
- abandon：仅 DRAFT/SAMPLE_GENERATING/AWAITING_SAMPLE_CONFIRMATION → ABANDONED。
- retry：仅 FAILED → 关联新任务（可沿用已确认样卡）；历史遗留任务（project/file
  缺失）只读不可重试。
- delete：仅终态任务（COMPLETED/FAILED/ABANDONED）；STAGED 残留卡随删除级联清理
  （绝不转无来源可见卡），delete_generated_cards 决定已发布卡去留（保留 →
  卡片 source_task_id SET NULL）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
状态转移用 DB 条件更新（并发转移不互相覆盖），非法前置统一 TASK_STATE_CONFLICT。
"""

import json
import uuid
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import GenerationConfig
from domain.enums import TaskStatus
from domain.task import TERMINAL_TASK_STATUSES
from infra.db.models import (
    ApiKey,
    Card,
    Chapter,
    GenerationOperation,
    LearningProject,
    LlmCallAttempt,
    Material,
    Task,
)
from services.decks.service import _owned as _owned_deck
from services.generation.quota import estimate_task_units, task_unit_budget
from services.generation.samples import config_fingerprint
from services.generation.validate import validate_config
from services.pdf.text_chunks import load_pages
from services.tasks.lease import TaskLease, require_lease
from services.tasks.operations import (
    begin_operation,
    bind_operation_task,
    finish_operation,
    normalized_input_fingerprint,
)


def _uuid4() -> str:
    return str(uuid.uuid4())


def _owned_task(session: Session, *, user_id: str, task_id: str) -> Task:
    """任务归属校验（404）。populate_existing：同 session 内 Core 条件更新/外部写入后
    identity map 可能停留旧值（expire_on_commit=False），状态机前置读必须取 DB 权威。"""
    task = session.get(Task, task_id, populate_existing=True)
    if task is None or task.user_id != user_id:
        raise AppError(ErrorCode.TASK_NOT_FOUND, "任务不存在")
    return task


def _owned_project(session: Session, *, user_id: str, project_id: str) -> LearningProject:
    """项目归属校验（与 projects.service 同口径）：不存在/跨用户 → 404 不暴露存在性。"""
    project = session.get(LearningProject, project_id)
    if project is None or project.user_id != user_id:
        raise AppError(ErrorCode.PROJECT_NOT_FOUND, "项目不存在或无权访问")
    return project


def _task_file_id(session: Session, *, project_id: str) -> str | None:
    """任务快照 file_id：项目首份 PDF 资料（多资料语义）；纯文本项目为 None（契约 3.4）。"""
    row = session.execute(
        select(Material.material_id)
        .where(Material.project_id == project_id, Material.type == "PDF")
        .order_by(Material.created_at, Material.material_id)
        .limit(1)
    ).first()
    return row[0] if row else None


def _snapshot_chapter_ids(task: Task) -> list[str]:
    """任务章节快照 → chapter_id 列表（JSON 解析失败/畸形 → 空列表）。"""
    try:
        snapshot = json.loads(task.selected_chapters)
    except (ValueError, TypeError):
        return []
    if not isinstance(snapshot, list):
        return []
    return [e["chapter_id"] for e in snapshot if isinstance(e, dict) and "chapter_id" in e]


def _chapter_snapshot(
    session: Session, *, project_id: str, chapter_ids: list[str]
) -> list[dict[str, object]]:
    """章节归属校验 + 快照（契约 3.4 Chapter[]；V25-D-29 多资料：material_id 入快照，
    TEXT 章节页码为 null；章节删除后名称从快照还原）。"""
    if not chapter_ids:
        raise AppError(ErrorCode.VALIDATION_ERROR, "章节列表不能为空")
    chapters = session.scalars(
        select(Chapter)
        .join(Material, Material.material_id == Chapter.material_id)
        .where(Chapter.chapter_id.in_(chapter_ids), Material.project_id == project_id)
    ).all()
    by_id = {ch.chapter_id: ch for ch in chapters}
    if any(cid not in by_id for cid in chapter_ids):
        raise AppError(ErrorCode.CHAPTER_NOT_FOUND, "章节不属于该项目资料")
    return [
        {
            "chapter_id": cid,
            "material_id": by_id[cid].material_id,
            "name": by_id[cid].name,
            "start_page": by_id[cid].start_page,
            "end_page": by_id[cid].end_page,
        }
        for cid in chapter_ids
    ]


def _require_same_project_deck(
    session: Session, *, user_id: str, project_id: str, deck_id: str
) -> None:
    """目标牌组必须属于同一项目（6.4）；不存在/他属/独立牌组 → 404 DECK_NOT_FOUND。"""
    deck = _owned_deck(session, user_id=user_id, deck_id=deck_id)
    if deck.project_id != project_id:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在或不属于该项目")


def _require_api_key(session: Session, *, user_id: str) -> None:
    """已保存 Key 校验（6.2：无 Key → API_KEY_NOT_SET）；只查 status=AVAILABLE 行存在，
    不解密（V5A 生成时才解密调用）。"""
    key_row = session.scalar(
        select(ApiKey.user_id).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
    )
    if key_row is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "未保存可用 API Key")


def _chapter_chars(session: Session, *, chapter_snapshot: list[dict[str, object]]) -> int:
    """所选章节的文本总量（密度制预算估算输入；查询异常/无文本时按 0 处理走回落估算）。"""
    total = 0
    for entry in chapter_snapshot:
        start = entry.get("start_page")
        end = entry.get("end_page")
        pages = load_pages(
            session,
            material_id=str(entry["material_id"]),
            start_page=int(str(start)) if start is not None else None,
            end_page=int(str(end)) if end is not None else None,
        )
        total += sum(page.char_count for page in pages)
    return total


def _budget_guard(
    session: Session,
    *,
    chapter_count: int,
    chapter_chars: int,
    config: GenerationConfig,
    settings: Settings | None,
) -> None:
    """预算硬上限（spec §10；V25-D-25 密度制口径）：按所选章节文本规模估算的区间上限
    超过任务单元硬顶 → 拒绝创建。文本不可得（异常）时回落旧口径 估算，由规划期组数
    上限兜底。"""
    if settings is None:
        injected = session.info.get("settings")
        settings = injected if isinstance(injected, Settings) else Settings()
    if chapter_chars > 0:
        estimated = estimate_task_units(chapter_chars, config.coverage_mode)
    else:
        estimated = int(task_unit_budget(chapter_count, config.coverage_mode) * 1.2)
    if estimated > settings.max_generation_units_per_task:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "生成单元预算超出上限（按所选章节内容规模估算）；请减少章节或改用更精简的覆盖模式",
        )


def _cas_transition(
    session: Session,
    *,
    task_id: str,
    from_statuses: frozenset[str],
    values: dict[str, Any],
    lease: TaskLease | None = None,
) -> bool:
    """状态转移条件更新（并发安全）：WHERE task_id AND status IN (前置)；rowcount=0 →
    前置不符或已转移。不 commit——由调用方提交。"""
    predicates: list[Any] = [Task.task_id == task_id, Task.status.in_(from_statuses)]
    if lease is not None:
        predicates.extend(
            [
                Task.claimed_by == lease.worker_id,
                Task.lease_token == lease.token,
                Task.lease_version == lease.version,
                Task.lease_until.is_not(None),
            ]
        )
    result = cast(
        CursorResult[Any],
        session.execute(update(Task).where(*predicates).values(**values)),
    )
    return result.rowcount == 1


def task_view(task: Task) -> dict[str, object]:
    """任务视图：selected_chapters/generation_config/cursor/sample_cards 从 JSON 反序列化；
    resumable bool；internal_stage 取自 stage 列（V2.5 改名 internal_stage 语义）。"""
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,  # V2.5 归属项目；迁移前历史任务可为 null
        "file_id": task.file_id,
        "deck_id": task.deck_id,
        "retry_of_task_id": task.retry_of_task_id,  # V2.5 失败重试关联
        "operation_id": task.operation_id,
        "status": task.status,
        "internal_stage": task.stage,  # V2.5：stage 列 → internal_stage 语义（运行期观测）
        "selected_chapters": json.loads(task.selected_chapters),
        "generation_config": json.loads(task.generation_config),
        "sample_cards": json.loads(task.sample_cards) if task.sample_cards else None,
        "sample_config_hash": task.sample_config_hash,
        "sample_confirmed_at": task.sample_confirmed_at,
        "cursor": json.loads(task.cursor) if task.cursor else None,
        "generated_card_count": task.generated_card_count,
        "total_batch_count": task.total_batch_count,
        "completed_batch_count": task.completed_batch_count,
        "resumable": bool(task.resumable),
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "completion_reason": task.completion_reason,
        "skipped_planning_group_count": task.skipped_planning_group_count,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "ended_at": task.ended_at,
        "updated_at": task.updated_at,
    }


def create_task(
    session: Session,
    *,
    user_id: str,
    project_id: str,
    deck_id: str,
    chapter_ids: list[str],
    config: GenerationConfig,
    now: str,
    settings: Settings | None = None,
    operation_key: str | None = None,
) -> Task:
    """建立 DRAFT（自动保存语义，6.4）：章节/牌组同项目归属校验 → 已保存 Key →
    预算硬上限 → 落 DRAFT（章节快照 + 目标牌组 + 配置；internal_stage 空）。
    页面切换/App 退出/换设备后经 GET 读取继续，无需重新上传 PDF。"""
    validate_config(config)
    project = _owned_project(session, user_id=user_id, project_id=project_id)
    chapter_snapshot = _chapter_snapshot(session, project_id=project_id, chapter_ids=chapter_ids)
    _require_same_project_deck(session, user_id=user_id, project_id=project_id, deck_id=deck_id)
    _require_api_key(session, user_id=user_id)
    _budget_guard(
        session,
        chapter_count=len(chapter_ids),
        chapter_chars=_chapter_chars(session, chapter_snapshot=chapter_snapshot),
        config=config,
        settings=settings,
    )
    stable_operation_key = operation_key or _uuid4()
    input_fingerprint = normalized_input_fingerprint(
        user_id=user_id,
        project_id=project_id,
        deck_id=deck_id,
        chapter_snapshot=chapter_snapshot,
        generation_config=config.model_dump(),
    )
    operation, existing_task, _deduplicated = begin_operation(
        session,
        user_id=user_id,
        operation_key=stable_operation_key,
        input_fingerprint=input_fingerprint,
        now=now,
    )
    if existing_task is not None:
        return existing_task
    task = Task(
        task_id=_uuid4(),
        user_id=user_id,
        project_id=project.project_id,
        file_id=_task_file_id(session, project_id=project.project_id),
        deck_id=deck_id,
        status=TaskStatus.DRAFT.value,
        stage=None,  # internal_stage 在正式生成（start）后才有观测值
        selected_chapters=json.dumps(chapter_snapshot, ensure_ascii=False),
        generation_config=json.dumps(config.model_dump(), ensure_ascii=False),
        generated_card_count=0,
        resumable=0,
        created_at=now,
        updated_at=now,
        operation_id=operation.operation_id,
    )
    session.add(task)
    session.flush()
    bind_operation_task(session, operation, task, now=now)
    return task


def get_task(session: Session, *, user_id: str, task_id: str) -> Task:
    return _owned_task(session, user_id=user_id, task_id=task_id)


def list_tasks(
    session: Session, *, user_id: str, project_id: str | None = None, status: str | None = None
) -> list[Task]:
    """任务列表（6.4 GET /tasks）：user 域 + 可选 project/status 过滤；project 跨用户
    → 404；status 非法 → 400。按 created_at 倒序（同毫秒以 task_id 次级键稳定）。"""
    stmt = select(Task).where(Task.user_id == user_id)
    if project_id is not None:
        _owned_project(session, user_id=user_id, project_id=project_id)
        stmt = stmt.where(Task.project_id == project_id)
    if status is not None:
        if status not in {s.value for s in TaskStatus}:
            raise AppError(ErrorCode.VALIDATION_ERROR, "非法任务状态筛选")
        stmt = stmt.where(Task.status == status)
    return list(session.scalars(stmt.order_by(Task.created_at.desc(), Task.task_id.desc())).all())


def update_task(
    session: Session,
    *,
    user_id: str,
    task_id: str,
    deck_id: str | None = None,
    chapter_ids: list[str] | None = None,
    config: GenerationConfig | None = None,
    now: str,
) -> Task:
    """配置变更（PATCH，幂等键兜底）：仅 DRAFT/AWAITING_SAMPLE_CONFIRMATION；任何变更
    使样卡失效 → DRAFT + 清空 sample_cards/sample_config_hash/sample_confirmed_at
    （4.1）。空 PATCH（无字段）→ 真 no-op（状态不转移、updated_at 不刷新）。"""
    task = _owned_task(session, user_id=user_id, task_id=task_id)
    if task.status not in (
        TaskStatus.DRAFT.value,
        TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value,
    ):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅 DRAFT/待确认样卡任务可修改配置")
    if deck_id is None and chapter_ids is None and config is None:
        return task
    project_id = task.project_id
    if project_id is None:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "历史遗留任务不可修改")
    # 变更目标校验（未变更字段沿用现值）
    new_deck = deck_id if deck_id is not None else task.deck_id
    if new_deck is None:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在或不属于该项目")
    new_chapter_ids = chapter_ids if chapter_ids is not None else _snapshot_chapter_ids(task)
    snapshot = _chapter_snapshot(session, project_id=project_id, chapter_ids=new_chapter_ids)
    if config is not None:
        validate_config(config)
        new_config = config
    else:
        try:
            new_config = GenerationConfig(**json.loads(task.generation_config))
        except (ValueError, TypeError):
            raise AppError(ErrorCode.INTERNAL_ERROR, "任务配置数据异常") from None
    _require_same_project_deck(session, user_id=user_id, project_id=project_id, deck_id=new_deck)
    _budget_guard(
        session,
        chapter_count=len(new_chapter_ids),
        chapter_chars=_chapter_chars(session, chapter_snapshot=snapshot),
        config=new_config,
        settings=None,
    )
    # 条件更新落库：并发 start/abandon 后状态已转移 → rowcount=0 不覆盖
    if not _cas_transition(
        session,
        task_id=task_id,
        from_statuses=frozenset(
            {TaskStatus.DRAFT.value, TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value}
        ),
        values={
            "deck_id": new_deck,
            "selected_chapters": json.dumps(snapshot, ensure_ascii=False),
            "generation_config": json.dumps(new_config.model_dump(), ensure_ascii=False),
            "status": TaskStatus.DRAFT.value,  # 修改后样卡失效 → 回 DRAFT（4.1）
            "sample_cards": None,
            "sample_config_hash": None,
            "sample_confirmed_at": None,
            "updated_at": now,
        },
    ):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务状态已变化，请刷新后重试")
    session.refresh(task)
    if task.operation_id is not None:
        operation = session.get(GenerationOperation, task.operation_id)
        if operation is not None:
            operation.input_fingerprint = normalized_input_fingerprint(
                user_id=user_id,
                project_id=project_id,
                deck_id=new_deck,
                chapter_snapshot=snapshot,
                generation_config=new_config.model_dump(),
            )
            operation.updated_at = now
    return task


def request_samples(session: Session, *, user_id: str, task_id: str, now: str) -> Task:
    """请求样卡（6.4）：仅 DRAFT → SAMPLE_GENERATING（4.1）；幂等键防重复触发。
    样卡由执行器 worker 后台生成并持久化（complete_samples）。"""
    task = _owned_task(session, user_id=user_id, task_id=task_id)
    if not _cas_transition(
        session,
        task_id=task_id,
        from_statuses=frozenset({TaskStatus.DRAFT.value}),
        values={"status": TaskStatus.SAMPLE_GENERATING.value, "updated_at": now},
    ):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅 DRAFT 任务可请求样卡")
    session.refresh(task)
    return task


def complete_samples(
    session: Session,
    *,
    task_id: str,
    cards: list[dict[str, object]],
    config_hash: str,
    now: str,
    lease: TaskLease | None = None,
) -> bool:
    """样卡 worker 完成（4.1）：条件更新 WHERE SAMPLE_GENERATING 持久化样卡 + 配置指纹
    → AWAITING_SAMPLE_CONFIRMATION。SAMPLE_GENERATING 时并发 abandon/转移 → rowcount=0
    不写入（后台请求完成写入无害、不复活终态）。返回是否写入。"""
    if lease is not None:
        require_lease(
            session,
            task_id=task_id,
            worker_id=lease.worker_id,
            token=lease.token,
            version=lease.version,
            now=now,
        )
    written = _cas_transition(
        session,
        task_id=task_id,
        from_statuses=frozenset({TaskStatus.SAMPLE_GENERATING.value}),
        values={
            "sample_cards": json.dumps(cards, ensure_ascii=False),
            "sample_config_hash": config_hash,
            "status": TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value,
            "claimed_by": None,
            "lease_token": None,
            "lease_until": None,
            "lease_version": (Task.lease_version + 1) if lease is not None else Task.lease_version,
            "updated_at": now,
        },
        lease=lease,
    )
    if written:
        finish_operation(session, task_id=task_id, status="ACTIVE", now=now)
    return written


def start_task(session: Session, *, user_id: str, task_id: str, now: str) -> Task:
    """start（6.4）：仅 AWAITING_SAMPLE_CONFIRMATION；校验当前配置 hash 与
    sample_config_hash 一致且样卡存在（失配/缺失 → 409 SAMPLE_STALE）；置
    sample_confirmed_at 并进入 GENERATING + internal_stage=PLANNING。"""
    task = _owned_task(session, user_id=user_id, task_id=task_id)
    if task.status != TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅待确认样卡任务可开始正式生成")
    try:
        config = GenerationConfig(**json.loads(task.generation_config))
        current_hash = config_fingerprint(config)
    except (ValueError, TypeError):
        current_hash = None
    if (
        task.sample_config_hash is None
        or current_hash is None
        or task.sample_config_hash != current_hash
        or not task.sample_cards
    ):
        raise AppError(ErrorCode.SAMPLE_STALE, "样卡已过期，请重新生成")
    if not _cas_transition(
        session,
        task_id=task_id,
        from_statuses=frozenset({TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value}),
        values={
            "status": TaskStatus.GENERATING.value,
            "stage": "PLANNING",  # internal_stage 起点（4.1 内部阶段依次 PLANNING→…）
            "sample_confirmed_at": now,
            "updated_at": now,
            "claimed_by": None,
            "lease_token": None,
            "lease_until": None,
            "lease_version": Task.lease_version + 1,
        },
    ):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务状态已变化，请刷新后重试")
    session.refresh(task)
    # Starting formal generation keeps the durable operation ACTIVE.  The operation is closed
    # only when the task reaches a terminal state (or is explicitly abandoned/deleted), so a
    # worker restart can still reconcile the same generation intent.
    finish_operation(session, task_id=task_id, status="ACTIVE", now=now)
    return task


def abandon_task(session: Session, *, user_id: str, task_id: str, now: str) -> Task:
    """abandon（4.1）：仅正式生成前状态（DRAFT/SAMPLE_GENERATING/AWAITING_SAMPLE_
    CONFIRMATION）→ ABANDONED 终态；SAMPLE_GENERATING 时后台样卡写入无害（CAS）。"""
    task = _owned_task(session, user_id=user_id, task_id=task_id)
    if not _cas_transition(
        session,
        task_id=task_id,
        from_statuses=frozenset(
            {
                TaskStatus.DRAFT.value,
                TaskStatus.SAMPLE_GENERATING.value,
                TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value,
            }
        ),
        values={
            "status": TaskStatus.ABANDONED.value,
            "stage": None,
            "ended_at": now,
            "resumable": 0,
            "updated_at": now,
            "claimed_by": None,
            "lease_token": None,
            "lease_until": None,
            "lease_version": Task.lease_version + 1,
        },
    ):
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅正式生成前任务可放弃")
    session.refresh(task)
    finish_operation(session, task_id=task_id, status="ABANDONED", now=now, reason="USER_ABANDON")
    return task


def retry_task(
    session: Session,
    *,
    user_id: str,
    task_id: str,
    now: str,
    settings: Settings | None = None,
    operation_key: str | None = None,
) -> Task:
    """失败重试（6.4/PRD V25-GEN-FR-07）：仅 FAILED；创建关联新任务（retry_of_task_id
    指向原任务），复制项目/PDF/牌组/章节/配置。已确认样卡沿用（正式生成失败 → 新任务
    直接可 start）；无已确认样卡（样卡阶段失败）→ 新任务 DRAFT 重新生成样卡。
    原失败任务保留；历史遗留任务（project/file 缺失）只读不可重试。"""
    original = _owned_task(session, user_id=user_id, task_id=task_id)
    if original.status != TaskStatus.FAILED.value:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅失败任务可重试")
    if original.project_id is None:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "历史遗留任务不可重试")
    if original.deck_id is None:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "目标牌组已删除，无法重试")
    try:
        config = GenerationConfig(**json.loads(original.generation_config))
    except (ValueError, TypeError):
        raise AppError(ErrorCode.INTERNAL_ERROR, "任务配置数据异常") from None
    validate_config(config)
    _require_api_key(session, user_id=user_id)
    _budget_guard(
        session,
        chapter_count=len(_snapshot_chapter_ids(original)),
        chapter_chars=_chapter_chars(
            session,
            chapter_snapshot=json.loads(original.selected_chapters or "[]")
            if isinstance(original.selected_chapters, str)
            else [],
        ),
        config=config,
        settings=settings,
    )
    try:
        snapshot = json.loads(original.selected_chapters)
        if not isinstance(snapshot, list):
            raise TypeError
    except (ValueError, TypeError):
        raise AppError(ErrorCode.INTERNAL_ERROR, "任务章节快照数据异常") from None
    operation, existing_task, _deduplicated = begin_operation(
        session,
        user_id=user_id,
        operation_key=operation_key or _uuid4(),
        input_fingerprint=normalized_input_fingerprint(
            user_id=user_id,
            project_id=original.project_id,
            deck_id=original.deck_id,
            chapter_snapshot=snapshot,
            generation_config=config.model_dump(),
            behavior_version="generation-retry-v1",
        ),
        now=now,
    )
    if existing_task is not None:
        return existing_task
    new_task = Task(
        task_id=_uuid4(),
        user_id=user_id,
        project_id=original.project_id,
        file_id=original.file_id,
        deck_id=original.deck_id,
        retry_of_task_id=original.task_id,
        status=TaskStatus.DRAFT.value,
        stage=None,
        selected_chapters=original.selected_chapters,
        generation_config=original.generation_config,
        generated_card_count=0,
        resumable=0,
        created_at=now,
        updated_at=now,
        operation_id=operation.operation_id,
    )
    if (
        original.sample_confirmed_at is not None
        and original.sample_config_hash is not None
        and original.sample_cards
    ):
        # 沿用已确认样卡（配置原样复制 → hash 一致）：新任务待确认可直接 start
        new_task.status = TaskStatus.AWAITING_SAMPLE_CONFIRMATION.value
        new_task.sample_cards = original.sample_cards
        new_task.sample_config_hash = original.sample_config_hash
        new_task.sample_confirmed_at = now
    session.add(new_task)
    session.flush()
    bind_operation_task(session, operation, new_task, now=now)
    return new_task


def delete_task(
    session: Session, *, user_id: str, task_id: str, delete_generated_cards: bool
) -> None:
    """删除任务历史（6.4）：仅终态任务；delete_generated_cards=true 连已发布卡删除
    （复习数据级联），false 只删任务行（cards.source_task_id FK SET NULL 保留卡）。

    V2.5（4.1 删除规则）：失败任务遗留的 STAGED 卡随任务删除级联清理——绝不转为
    无来源可见卡（STAGED 对用户不可见，删除任务后无主必清）；delete_generated_cards
    只决定已发布卡去留（保留 → source_task_id 置空 / 删除 → 连卡删除）。
    删除仅终态任务：与发布/worker 的并发由状态门卫串行化——worker 发布提交前任务
    仍 GENERATING（删除被 409 拒绝），发布后任务终态才可删（不复活）。
    """
    task = _owned_task(session, user_id=user_id, task_id=task_id)
    if task.status not in TERMINAL_TASK_STATUSES:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "仅终态任务可删除")
    for card in session.scalars(
        select(Card).where(Card.source_task_id == task_id, Card.publication_state == "STAGED")
    ).all():
        session.delete(card)
    published = session.scalars(
        select(Card).where(Card.source_task_id == task_id, Card.publication_state == "PUBLISHED")
    ).all()
    if delete_generated_cards:
        for card in published:
            session.delete(card)
    else:
        for card in published:
            card.source_task_id = None
    # Keep the cost/audit ledger after deleting task history.  The migration is compatible with
    # old SQLite files whose task FK was CASCADE because the explicit NULL happens before the
    # task DELETE; operation identity is retained but no longer points at a missing task row.
    session.execute(
        update(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id).values(task_id=None)
    )
    operation = session.scalar(
        select(GenerationOperation).where(GenerationOperation.task_id == task_id)
    )
    if operation is not None:
        operation_time = task.updated_at or operation.updated_at
        operation.task_id = None
        operation.updated_at = operation_time
        if operation.status == "ACTIVE":
            operation.status = "ABANDONED"
            operation.ended_at = operation_time
            operation.terminal_reason = "TASK_DELETE"
    session.delete(task)
