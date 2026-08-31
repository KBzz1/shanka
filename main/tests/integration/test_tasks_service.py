"""任务 service 集成测试（V2.5 Task 5）：项目域创建校验 + 归属守卫（真实 SQLite）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
pdf/deck/task/api_keys 均 FK → users——fixture 先建 users 行；tasks 创建校验 Key
需 ApiKey 种子。V2.5：create_task 以 project_id 为入口（POST /projects/{id}/tasks），
七态状态机完整转移表见 test_v25_task_lifecycle.py（本文件聚焦创建校验）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import (
    ApiKey,
    Base,
    Chapter,
    Deck,
    KnowledgePoint,
    LearningProject,
    Material,
    PdfFile,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from services.tasks.service import create_task, get_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


_NOW = "2026-08-15T00:00:00.000Z"


def _seed_context(session: Session, *, user_id: str, with_key: bool = True) -> dict[str, Any]:
    """users 前置 + 项目（PDF/2 章节/项目绑定牌组）+ ApiKey 种子（tasks 校验 Key）。"""
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
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    session.add(
        Material(
            material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project.project_id,
            type="PDF",
            name="seed.pdf",
            status=None,
            created_at=_NOW,
        )
    )
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
    chapters = []
    for i in range(2):
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=pdf.file_id,
            material_id=pdf.file_id,
            name=f"第{i + 1}章",
            start_page=i + 1,
            end_page=i + 2,
        )
        session.add(ch)
        session.flush()
        chapter_ids.append(ch.chapter_id)
        chapters.append(ch)
    if with_key:
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key="enc",
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
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "material_id": ch.material_id,
                "name": ch.name,
                "start_page": ch.start_page,
                "end_page": ch.end_page,
            }
            for ch in chapters
        ],
    }


def _config(tendency: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        coverage_mode=tendency,
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )


def test_tasks_create_draft_snapshot_without_planning(
    session_factory: Callable[[], Session],
) -> None:
    """V2.5：create_task 只落 DRAFT 创建快照（不规划）；selected_chapters 契约 3.4
    Chapter[] 可还原；generation_config JSON 持久化。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=user,
            project_id=str(ctx["project_id"]),
            deck_id=str(ctx["deck_id"]),
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now=_NOW,
        )
        session.commit()
        task_id = task.task_id
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        status = task.status
        stage = task.stage
    assert status == "DRAFT"
    assert stage is None  # internal_stage 正式生成前不暴露
    assert len(kps) == 0  # 规划由 worker 在 start 后接管（spec §6.1）
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.generation_config  # JSON 快照持久化
        assert json.loads(row.selected_chapters) == ctx["chapters"]


def test_tasks_create_without_key_422(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user, with_key=False)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=user,
            project_id=str(ctx["project_id"]),
            deck_id=str(ctx["deck_id"]),
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET


def test_tasks_create_cross_user_project_404(session_factory: Callable[[], Session]) -> None:
    """跨用户项目 → 404 PROJECT_NOT_FOUND（6.2 统一 404，不暴露存在性）。"""
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
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.PROJECT_NOT_FOUND


def test_tasks_create_foreign_chapter_404(session_factory: Callable[[], Session]) -> None:
    """章节归属校验：chapter_ids 含不属于项目资料的章节 → CHAPTER_NOT_FOUND
    （V25-D-29 多资料：归属经 materials join 校验）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        foreign = _uuid()
        session.add(
            User(
                user_id=foreign,
                username=f"u-{foreign[:8]}",
                email=f"u-{foreign[:8]}@example.com",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()
        other_project = LearningProject(
            project_id=_uuid(),
            user_id=foreign,
            name="他人项目",
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(other_project)
        session.flush()
        other_pdf = PdfFile(
            file_id=_uuid(),
            user_id=foreign,
            filename="c.pdf",
            storage_key=_uuid(),
            size_bytes=1,
            status="PARSED",
            created_at=_NOW,
        )
        session.add(other_pdf)
        session.flush()
        session.add(
            Material(
                material_id=other_pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=other_project.project_id,
                type="PDF",
                name="c.pdf",
                status=None,
                size_bytes=1,
                created_at=_NOW,
            )
        )
        session.flush()
        other_ch = Chapter(
            chapter_id=_uuid(),
            file_id=other_pdf.file_id,
            material_id=other_pdf.file_id,
            name="他章",
            start_page=1,
            end_page=2,
        )
        session.add(other_ch)
        session.flush()
        foreign_id = other_ch.chapter_id
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=user,
            project_id=str(ctx["project_id"]),
            deck_id=str(ctx["deck_id"]),
            chapter_ids=[ctx["chapter_ids"][0], foreign_id],
            config=_config(),
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.CHAPTER_NOT_FOUND


def test_tasks_get_missing_404(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_task(session, user_id=user, task_id=_uuid())
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_tasks_get_cross_user_404(session_factory: Callable[[], Session]) -> None:
    """任务归属守卫：他人任务 → 404（不暴露存在性）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=user,
            project_id=str(ctx["project_id"]),
            deck_id=str(ctx["deck_id"]),
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now=_NOW,
        )
        session.commit()
        task_id = task.task_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_task(session, user_id=_uuid(), task_id=task_id)
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND
