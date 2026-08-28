"""V2.5 任务生命周期集成测试（Task 5）：七态状态机 + 持久配置 + 放弃/重试/删除。

覆盖（structure-contract 4.1 状态机 + 6.4 接口语义）：
- 全量合法/非法转移表（状态门卫统一 TASK_STATE_CONFLICT；start 过期样卡 SAMPLE_STALE）；
- 自动保存：创建即 DRAFT，快照/配置跨 session 持久（页面切换/App 退出/换设备后继续）；
- 样卡 worker 后台完成（SAMPLE_GENERATING → AWAITING_SAMPLE_CONFIRMATION）；
- 配置变更使样卡失效（→ DRAFT + 清空 sample_cards/hash/confirmed_at）；
- abandon 只允许正式生成前状态；retry 失败任务关联新任务（可沿用已确认样卡）；
- delete 只允许终态任务；I-1 回归：任何服务路径不写迁移期旧状态（PENDING/RUNNING/...）。
"""

import inspect
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from domain.enums import TaskStatus
from domain.task import ACTIVE_TASK_STATUSES as _ACTIVE_TASK_STATUSES
from infra.db.models import (
    ApiKey,
    Base,
    Card,
    Chapter,
    Deck,
    LearningProject,
    PdfFile,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from services.generation.samples import config_fingerprint
from services.pdf.text_chunks import persist_text_chunks
from infra.llm.crypto import encrypt_key, key_from_settings
from services.tasks.service import (
    abandon_task,
    create_task,
    delete_task,
    list_tasks,
    request_samples,
    retry_task,
    start_task,
    update_task,
)

_NOW = "2026-08-15T00:00:00.000Z"

# V2.5 七态（合法值域；结构契约 4.1）
_SEVEN_STATES = {s.value for s in TaskStatus}
# 迁移期旧任务状态（V2.5 前运行时写入；I-1 回归断言禁用）
_LEGACY_STATES = {"PENDING", "RUNNING", "PAUSED", "CANCELLED"}


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v25_lifecycle.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _config(coverage_mode: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        coverage_mode=coverage_mode,
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )


def _config2() -> GenerationConfig:
    """与 _config() 不同（样卡 hash 失配场景）。"""
    return GenerationConfig(
        coverage_mode="EXTENSIVE",
        difficulty_ratio=DifficultyRatio(basic=50, understanding=30, deep_question=20),
    )


def _seed_context(session: Session, *, user_id: str, with_key: bool = True) -> dict[str, object]:
    """users 前置 + 项目（PdfFile PARSED + 2 章节 + 项目绑定牌组）+ ApiKey 种子。"""
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=10,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    deck = Deck(
        deck_id=_uuid(),
        user_id=user_id,
        name="D",
        source="MANUAL",
        project_id=project.project_id,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(deck)
    session.flush()
    chapter_ids: list[str] = []
    for i in range(2):
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=pdf.file_id,
            name=f"第{i + 1}章",
            start_page=i + 1,
            end_page=i + 2,
        )
        session.add(ch)
        session.flush()
        chapter_ids.append(ch.chapter_id)
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now=_NOW,
    )
    if with_key:
        encryption_key = key_from_settings(_ensure_settings(session))
        assert encryption_key is not None
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=encrypt_key("sk-test-0123456789", encryption_key),
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
    session.flush()
    return {
        "project_id": project.project_id,
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
    }


def _create_draft(session: Session, ctx: dict[str, object], *, user_id: str) -> Task:
    return create_task(
        session,
        user_id=user_id,
        project_id=str(ctx["project_id"]),
        deck_id=str(ctx["deck_id"]),
        chapter_ids=[str(c) for c in cast(list[str], ctx["chapter_ids"])],
        config=_config(),
        now=_NOW,
    )


def _put_state(
    session: Session, task_id: str, *, status: str, samples: bool = False, confirmed: bool = False
) -> None:
    """直写任务到指定状态（测试前置；样卡可选，确认样卡时带 hash）。"""
    values: dict[str, object] = {
        "status": status,
        "sample_cards": None,
        "sample_config_hash": None,
        "sample_confirmed_at": None,
    }
    if samples:
        values["sample_cards"] = json.dumps(
            [{"card_id": _uuid(), "front": "f", "back": "b", "card_type": "QUESTION"}],
            ensure_ascii=False,
        )
        values["sample_config_hash"] = config_fingerprint(_config())
    if confirmed:
        values["sample_confirmed_at"] = _NOW
    session.execute(update(Task).where(Task.task_id == task_id).values(**values))
    session.commit()


# ---------- 状态转移表（合法 + 非法全量） ----------


def test_state_transition_table(
    session_factory: Callable[[], Session],
) -> None:
    """4.1 状态机全量转移表：每状态 × 每用户操作 → 期望结果。

    - 合法转移（正表）与非法前置（统一 TASK_STATE_CONFLICT，start 另有 SAMPLE_STALE）；
    - 断言状态门卫集中（服务层抛错码统一），且任何写入不落旧状态。
    """
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    # 每行：(操作名, 前置状态, 期望错误码 或 None=合法, 期望后置状态)
    table: list[tuple[str, str, ErrorCode | None, str]] = [
        # samples 请求：仅 DRAFT 合法 → SAMPLE_GENERATING
        ("samples", "DRAFT", None, "SAMPLE_GENERATING"),
        ("samples", "SAMPLE_GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("samples", "AWAITING_SAMPLE_CONFIRMATION", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("samples", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("samples", "COMPLETED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("samples", "FAILED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("samples", "ABANDONED", ErrorCode.TASK_STATE_CONFLICT, ""),
        # 配置变更：DRAFT/AWAITING 合法 → DRAFT（样卡失效）
        ("patch", "DRAFT", None, "DRAFT"),
        ("patch", "SAMPLE_GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("patch", "AWAITING_SAMPLE_CONFIRMATION", None, "DRAFT"),
        ("patch", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("patch", "COMPLETED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("patch", "FAILED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("patch", "ABANDONED", ErrorCode.TASK_STATE_CONFLICT, ""),
        # start：仅 AWAITING（样卡有效）合法 → GENERATING；过期样卡 → SAMPLE_STALE
        ("start", "DRAFT", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("start", "SAMPLE_GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("start", "AWAITING_SAMPLE_CONFIRMATION", None, "GENERATING"),
        ("start", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("start", "COMPLETED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("start", "FAILED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("start", "ABANDONED", ErrorCode.TASK_STATE_CONFLICT, ""),
        # abandon：仅正式生成前状态合法 → ABANDONED
        ("abandon", "DRAFT", None, "ABANDONED"),
        ("abandon", "SAMPLE_GENERATING", None, "ABANDONED"),
        ("abandon", "AWAITING_SAMPLE_CONFIRMATION", None, "ABANDONED"),
        ("abandon", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("abandon", "COMPLETED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("abandon", "FAILED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("abandon", "ABANDONED", ErrorCode.TASK_STATE_CONFLICT, ""),
        # retry：仅 FAILED 合法（新任务创建见专测）
        ("retry", "DRAFT", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("retry", "SAMPLE_GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("retry", "AWAITING_SAMPLE_CONFIRMATION", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("retry", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("retry", "COMPLETED", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("retry", "FAILED", None, "DRAFT"),  # 无已确认样卡 → 新任务 DRAFT
        ("retry", "ABANDONED", ErrorCode.TASK_STATE_CONFLICT, ""),
        # delete：仅终态合法
        ("delete", "DRAFT", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("delete", "SAMPLE_GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("delete", "AWAITING_SAMPLE_CONFIRMATION", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("delete", "GENERATING", ErrorCode.TASK_STATE_CONFLICT, ""),
        ("delete", "COMPLETED", None, ""),
        ("delete", "FAILED", None, ""),
        ("delete", "ABANDONED", None, ""),
    ]
    with session_factory() as session:
        for i, (op, pre, expected_code, expected_status) in enumerate(table):
            # 每行独立任务：避免行间互相干扰
            task = _create_draft(session, ctx, user_id=user)
            session.commit()
            task_id = task.task_id
            # start 行需有效样卡（AWAITING 前置）；其余行无样卡前置
            _put_state(
                session,
                task_id,
                status=pre,
                samples=(op == "start" and pre == "AWAITING_SAMPLE_CONFIRMATION"),
            )
            try:
                if op == "samples":
                    result = request_samples(session, user_id=user, task_id=task_id, now=_NOW)
                elif op == "patch":
                    result = update_task(
                        session, user_id=user, task_id=task_id, config=_config2(), now=_NOW
                    )
                elif op == "start":
                    result = start_task(session, user_id=user, task_id=task_id, now=_NOW)
                elif op == "abandon":
                    result = abandon_task(session, user_id=user, task_id=task_id, now=_NOW)
                elif op == "retry":
                    result = retry_task(session, user_id=user, task_id=task_id, now=_NOW)
                else:
                    delete_task(
                        session, user_id=user, task_id=task_id, delete_generated_cards=False
                    )
                    session.commit()
                    row = session.get(Task, task_id)
                    assert row is None, f"delete 后任务行应消失 (pre={pre})"
                    continue
                session.commit()
            except AppError as exc:
                assert expected_code is not None, f"{op}/{pre}: 不应抛错，实际 {exc.code}"
                assert exc.code is expected_code, f"{op}/{pre}: {exc.code} != {expected_code}"
                session.rollback()
                continue
            assert expected_code is None, f"{op}/{pre}: 应抛 {expected_code}，实际成功"
            assert result.status == expected_status, f"{op}/{pre}: {result.status}"
            # 数据库审计：任何路径写入的状态必须落在七态内（I-1 回归）
            row = session.get(Task, task_id)
            assert row is not None and row.status in _SEVEN_STATES


# ---------- 创建与自动保存 ----------


def test_create_task_draft_snapshot_durable_across_sessions(
    session_factory: Callable[[], Session],
) -> None:
    """创建即 DRAFT（自动保存）：章节快照/目标牌组/配置持久化；新 session（页面切换/
    App 退出/换设备）读取继续，无需重新上传 PDF。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        assert task.status == "DRAFT"
        assert task.stage is None  # internal_stage 在正式生成前不暴露
        assert task.resumable == 0
        task_id = task.task_id
    with session_factory() as session:  # "新页面"：全新 session 读取
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "DRAFT"
        assert row.project_id == ctx["project_id"]
        assert row.deck_id == ctx["deck_id"]
        snapshot = json.loads(row.selected_chapters)
        assert [s["chapter_id"] for s in snapshot] == ctx["chapter_ids"]
        assert json.loads(row.generation_config) == _config().model_dump()
        assert row.sample_cards is None


def test_create_task_same_project_deck_required(
    session_factory: Callable[[], Session],
) -> None:
    """目标牌组必须属于同一项目（6.4：deck_id 同项目校验）→ 他项目牌组 DECK_NOT_FOUND。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        other_pdf = PdfFile(
            file_id=_uuid(),
            user_id=user,
            filename="c.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at=_NOW,
        )
        session.add(other_pdf)
        session.flush()
        other_project = LearningProject(
            project_id=_uuid(),
            user_id=user,
            file_id=other_pdf.file_id,
            name="P2",
            chapters_confirmed_at=None,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(other_project)
        session.flush()
        other_deck = Deck(
            deck_id=_uuid(),
            user_id=user,
            name="D2",
            source="MANUAL",
            project_id=other_project.project_id,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(other_deck)
        session.commit()
        foreign_deck_id = other_deck.deck_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=user,
            project_id=str(ctx["project_id"]),
            deck_id=foreign_deck_id,
            chapter_ids=[str(c) for c in cast(list[str], ctx["chapter_ids"])],
            config=_config(),
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_create_task_cross_user_project_404(session_factory: Callable[[], Session]) -> None:
    """跨用户项目 → 统一 404 PROJECT_NOT_FOUND（6.2 不暴露存在性）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=_uuid(),
            project_id=str(ctx["project_id"]),
            deck_id=str(ctx["deck_id"]),
            chapter_ids=[str(c) for c in cast(list[str], ctx["chapter_ids"])],
            config=_config(),
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.PROJECT_NOT_FOUND


# ---------- start 的样卡 hash 校验 ----------


def test_start_stale_hash_409(session_factory: Callable[[], Session]) -> None:
    """配置变更后仍尝试确认旧样卡 → 409 SAMPLE_STALE（4.1）；无样卡同样 SAMPLE_STALE。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
    # AWAITING + 配置与样卡 hash 失配（_put_state 落的是 _config 的 hash，先改配置）
    with session_factory() as session:
        update_task(session, user_id=user, task_id=task_id, config=_config2(), now=_NOW)
        session.commit()
        _put_state(session, task_id, status="AWAITING_SAMPLE_CONFIRMATION", samples=True)
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        start_task(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.SAMPLE_STALE
    # 无样卡（hash 空）→ SAMPLE_STALE
    with session_factory() as session:
        _put_state(session, task_id, status="AWAITING_SAMPLE_CONFIRMATION", samples=False)
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        start_task(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.SAMPLE_STALE


def test_start_sets_confirmed_at_and_planning_stage(
    session_factory: Callable[[], Session],
) -> None:
    """start 成功：置 sample_confirmed_at 并进入 GENERATING + internal_stage=PLANNING。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _put_state(session, task_id, status="AWAITING_SAMPLE_CONFIRMATION", samples=True)
    with session_factory() as session:
        result = start_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert result.status == "GENERATING"
        assert result.stage == "PLANNING"
        assert result.sample_confirmed_at == _NOW
        # start 幂等守卫：已 GENERATING 再 start → 409
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        start_task(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT


# ---------- 配置变更使样卡失效 ----------


def test_update_config_invalidates_prior_sample(
    session_factory: Callable[[], Session],
) -> None:
    """配置变更（DRAFT/AWAITING）→ DRAFT + sample_cards/sample_config_hash/sample_confirmed_at 清空。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _put_state(session, task_id, status="AWAITING_SAMPLE_CONFIRMATION", samples=True)
    with session_factory() as session:
        result = update_task(session, user_id=user, task_id=task_id, config=_config2(), now=_NOW)
        session.commit()
        assert result.status == "DRAFT"
        assert result.sample_cards is None
        assert result.sample_config_hash is None
        assert result.sample_confirmed_at is None
        assert json.loads(result.generation_config) == _config2().model_dump()  # 新配置已持久化
    # 空 PATCH（无字段）→ 真 no-op：状态不转移、updated_at 不刷新
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        before = task.updated_at
    with session_factory() as session:
        result = update_task(session, user_id=user, task_id=task_id, now=_NOW)
        assert result.status == "DRAFT"
        assert result.updated_at == before


# ---------- abandon / retry / delete ----------


def test_abandon_sets_terminal_state(session_factory: Callable[[], Session]) -> None:
    """abandon → ABANDONED 终态 + ended_at；已终态再 abandon → 409。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = abandon_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert result.status == "ABANDONED"
        assert result.ended_at == _NOW
        assert result.resumable == 0
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        abandon_task(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT


def test_retry_carries_confirmed_samples_to_awaiting(
    session_factory: Callable[[], Session],
) -> None:
    """正式生成失败重试：新任务沿用已确认样卡（4.1），状态 AWAITING_SAMPLE_CONFIRMATION
    可直接 start；retry_of_task_id 指向原任务。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _put_state(session, task_id, status="FAILED", samples=True, confirmed=True)
    with session_factory() as session:
        new_task = retry_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert new_task.status == "AWAITING_SAMPLE_CONFIRMATION"
        assert new_task.retry_of_task_id == task_id
        assert new_task.sample_cards is not None
        assert new_task.sample_config_hash == config_fingerprint(_config())
        assert new_task.sample_confirmed_at == _NOW
        # 沿用样卡 + 同配置 → 可直接 start
        started = start_task(session, user_id=user, task_id=new_task.task_id, now=_NOW)
        session.commit()
        assert started.status == "GENERATING"
    # 原失败任务保留
    with session_factory() as session:
        original = session.get(Task, task_id)
        assert original is not None and original.status == "FAILED"


def test_retry_without_confirmed_samples_creates_draft(
    session_factory: Callable[[], Session],
) -> None:
    """样卡阶段失败（无已确认样卡）重试：新任务 DRAFT，重新生成样卡。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _put_state(session, task_id, status="FAILED")
    with session_factory() as session:
        new_task = retry_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert new_task.status == "DRAFT"
        assert new_task.retry_of_task_id == task_id
        assert new_task.sample_cards is None
        assert new_task.sample_config_hash is None
        assert new_task.project_id == ctx["project_id"]
        assert json.loads(new_task.generation_config) == _config().model_dump()


def test_retry_legacy_orphan_task_not_retryable(
    session_factory: Callable[[], Session],
) -> None:
    """迁移前失去 PDF 的终态历史任务（project_id null）→ 只读不可重试。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _put_state(session, task_id, status="FAILED")
        session.execute(
            update(Task).where(Task.task_id == task_id).values(project_id=None, file_id=None)
        )
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        retry_task(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT


def test_delete_terminal_task_cards_optional(
    session_factory: Callable[[], Session],
) -> None:
    """终态任务可删：delete_generated_cards=true 连已发布卡（source_task_id）删除；
    false 只删任务历史（cards.source_task_id SET NULL 保留卡）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        card = Card(
            card_id=_uuid(),
            user_id=user,
            deck_id=str(ctx["deck_id"]),
            position=1,
            front="f",
            back="b",
            source="GENERATED",
            card_type="QUESTION",
            source_task_id=task_id,
            publication_state="PUBLISHED",
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(card)
        session.commit()
        card_id = card.card_id
        _put_state(session, task_id, status="COMPLETED")
    # 保留卡片：任务删、卡保留（source_task_id 置空）
    with session_factory() as session:
        delete_task(session, user_id=user, task_id=task_id, delete_generated_cards=False)
        session.commit()
        assert session.get(Task, task_id) is None
        row = session.get(Card, card_id)
        assert row is not None and row.source_task_id is None


def test_delete_terminal_with_cards(
    session_factory: Callable[[], Session],
) -> None:
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        card = Card(
            card_id=_uuid(),
            user_id=user,
            deck_id=str(ctx["deck_id"]),
            position=1,
            front="f",
            back="b",
            source="GENERATED",
            card_type="QUESTION",
            source_task_id=task_id,
            publication_state="PUBLISHED",
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(card)
        session.commit()
        card_id = card.card_id
        _put_state(session, task_id, status="COMPLETED")
    with session_factory() as session:
        delete_task(session, user_id=user, task_id=task_id, delete_generated_cards=True)
        session.commit()
        assert session.get(Task, task_id) is None
        assert session.get(Card, card_id) is None


# ---------- 列表 ----------


def test_list_tasks_filters_by_project_and_status(
    session_factory: Callable[[], Session],
) -> None:
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        t1 = _create_draft(session, ctx, user_id=user)
        session.commit()
        _put_state(session, t1.task_id, status="COMPLETED")
        _create_draft(session, ctx, user_id=user)  # 第二个 DRAFT 任务（列表断言计数）
        session.commit()
    with session_factory() as session:
        all_tasks = list_tasks(session, user_id=user)
        assert len(all_tasks) == 2
        by_status = list_tasks(session, user_id=user, status="COMPLETED")
        assert [t.task_id for t in by_status] == [t1.task_id]
        by_project = list_tasks(session, user_id=user, project_id=str(ctx["project_id"]))
        assert len(by_project) == 2
        # 跨用户项目 → 404（不暴露存在性）
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        list_tasks(session, user_id=_uuid(), project_id=str(ctx["project_id"]))
    assert excinfo.value.code is ErrorCode.PROJECT_NOT_FOUND


# ---------- I-1 回归：服务路径禁止写迁移期旧状态 ----------


def test_no_service_path_writes_legacy_task_status(
    session_factory: Callable[[], Session],
) -> None:
    """I-1（Task 2 review）回归：服务路径前半程生命周期（创建→样卡→start 至
    GENERATING，含 abandon/重试/删除/配置变更）数据库任务状态全程只落七态；
    GENERATING→COMPLETED 的执行路径由 test_tasks_api 轮询用例驱动，静态扫描
    兜底——四个运行时模块的 Task 状态写入点禁用 PENDING/RUNNING/PAUSED/CANCELLED。"""
    from services.generation import planning_executor, scoring
    from services.tasks import executor as tasks_executor
    from services.tasks import service as tasks_service

    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    observed: list[str] = []

    def _assert_seven(session: Session) -> None:
        statuses = set(session.scalars(select(Task.status)).all())
        observed.extend(statuses)
        assert statuses <= _SEVEN_STATES, f"发现旧状态写入: {statuses - _SEVEN_STATES}"

    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        _assert_seven(session)
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        _assert_seven(session)
        # 样卡 worker 扫描完成（SAMPLE_GENERATING → AWAITING）
        tasks_executor.process_active_tasks(session, settings=_ensure_settings(session), client_factory=_stub_factory())
        session.commit()
        _assert_seven(session)
        start_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        _assert_seven(session)
    # 弃权路径
    with session_factory() as session:
        task2 = _create_draft(session, ctx, user_id=user)
        session.commit()
        abandon_task(session, user_id=user, task_id=task2.task_id, now=_NOW)
        session.commit()
        _assert_seven(session)
    # 重试路径（把任务直写 FAILED 后 retry）
    with session_factory() as session:
        _put_state(session, task2.task_id, status="FAILED", samples=True, confirmed=True)
        retry_task(session, user_id=user, task_id=task2.task_id, now=_NOW)
        session.commit()
        _assert_seven(session)
    # 静态扫描：运行时模块的 Task 状态写入/守卫不得出现旧状态字面量
    for module in (tasks_service, tasks_executor, planning_executor, scoring):
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if any(
                marker in stripped
                for marker in (
                    "Task.status ==",
                    "Task.status !=",
                    "values(status=",
                    "task.status =",
                    "task.status !=",
                )
            ):
                assert not any(legacy in stripped for legacy in _LEGACY_STATES), (
                    f"{module.__name__} 写入/守卫旧任务状态: {stripped}"
                )


def _ensure_settings(session: Session) -> Settings:
    """执行器缺省 settings：session.info 注入（executor 定式），避免触网配置读取。"""
    # _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
    settings = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
    session.info["settings"] = settings
    return settings


class _StubClient:
    """样卡 worker 注入的假 LLM：按信封 target_difficulty 返回合规 QUESTION 卡（不触网）。"""

    def close(self) -> None:
        pass

    def chat(
        self, user_prompt: str, system_prompt: str | None = None, max_tokens: int | None = None
    ) -> dict[str, object]:
        import json as _json

        raw = user_prompt.split("<GENERATOR_INPUT>")[1].split("</GENERATOR_INPUT>")[0]
        difficulty = str(_json.loads(raw)["target_difficulty"])
        return {
            "content": _json.dumps(
                {
                    "cards": [
                        {
                            "type": "QUESTION",
                            "question": f"样卡问题-{difficulty}",
                            "answer": f"样卡答案-{difficulty}",
                        }
                    ]
                }
            ),
            "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
            "model": "deepseek-v4-flash",
            "http_status": 200,
            "duration_ms": 1,
        }


def _stub_factory() -> Callable[[str], _StubClient]:
    return lambda _key: _StubClient()


# ---------- 并发/恢复：样卡任务跨"重启"续跑 ----------


def test_sample_task_resumes_after_restart(session_factory: Callable[[], Session]) -> None:
    """App 退出/进程重启后 SAMPLE_GENERATING 任务由下一轮扫描继续完成（后台连续性）。"""
    from services.tasks.executor import process_active_tasks

    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
    # "重启"：新 session 显式扫描
    with session_factory() as session:
        process_active_tasks(session, settings=_ensure_settings(session), client_factory=_stub_factory())
        session.commit()
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "AWAITING_SAMPLE_CONFIRMATION"
        assert row.sample_cards is not None
        assert row.sample_config_hash == config_fingerprint(_config())


# ---------- 收敛断言（Task 4 汇合点） ----------


def test_active_task_statuses_converged_to_seven_state_non_terminals() -> None:
    """Task 4 汇合点：运行期只写七态后，项目删除保护只保留七态非终态（无迁移期旧态）。"""
    assert _ACTIVE_TASK_STATUSES == {
        "DRAFT",
        "SAMPLE_GENERATING",
        "AWAITING_SAMPLE_CONFIRMATION",
        "GENERATING",
    }
    assert _ACTIVE_TASK_STATUSES & _LEGACY_STATES == set()


def test_generation_config_has_no_card_count_estimate() -> None:
    """API 配置不含任何卡数估算字段（PRD：样卡界面不展示预计总卡数）。"""
    from app.schemas.samples import GenerationConfig as ConfigSchema

    assert set(ConfigSchema.model_fields) == {
        "coverage_mode",
        "difficulty_ratio",
        "custom_requirements",
    }
