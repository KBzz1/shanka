"""任务 service 集成测试：创建/状态机/取消/resume/校验（真实 SQLite + fake 执行）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
pdf/deck/task/api_keys 均 FK → users——fixture 先建 users 行
（HTTP 流中由注册端点建立，本层显式补种）；tasks 校验 Key 需 ApiKey 种子。
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
from infra.db.models import ApiKey, Base, Chapter, KnowledgePoint, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.tasks.service import cancel_task, create_task, get_task, resume_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_context(session: Session, *, user_id: str, with_key: bool = True) -> dict[str, Any]:
    """users 前置 + PDF/章节/牌组 + ApiKey 种子（tasks 校验 Key）。

    PDF/牌组/ApiKey 均 user 域（P4-3/P4-4 切换）。
    """
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            password_hash="x",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    chapter_ids = []
    chapters = []
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
        chapters.append(ch)
    if with_key:
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key="enc",
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
    session.flush()
    return {
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "name": ch.name,
                "start_page": ch.start_page,
                "end_page": ch.end_page,
            }
            for ch in chapters
        ],
    }


def _config(tendency: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        quantity_tendency=tendency,
        difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
    )


def test_tasks_create_pending_snapshot_without_planning(
    session_factory: Callable[[], Session],
) -> None:
    """T8 新语义：create_task 只落创建快照（PENDING + stage=PLANNING，不自动规划）。

    原验收意图（创建快照持久化 + selected_chapters 契约 3.4 Chapter[] 可还原）保留；
    知识点规划由规划 worker CAS 接管（spec §6.1），创建事务内不再产出 KnowledgePoint
    （断言 0 行——规划断言载体换成 test_planning_executor.py 的 claim 流程）。
    """
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        task_id = task.task_id
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        status = task.status
        stage = task.stage
    assert status == "PENDING"
    assert stage == "PLANNING"
    assert len(kps) == 0  # 规划不再在创建同事务（spec §6.1）
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.generation_config  # JSON 快照持久化
        # selected_chapters 快照存完整 Chapter 对象（契约 3.4 Chapter[]；3.6 名称可还原）
        snapshot = json.loads(row.selected_chapters)
        assert snapshot == ctx["chapters"]


def test_tasks_create_without_key_422(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device, with_key=False)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET


def test_tasks_create_cross_user_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=_uuid(),
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_tasks_create_foreign_chapter_404(session_factory: Callable[[], Session]) -> None:
    """章节归属校验：chapter_ids 含不属于该 PDF 的章节 → PDF_NOT_FOUND（与 samples 一致）。"""
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        other_owner = _uuid()
        session.add(
            User(
                user_id=other_owner,
                username=f"u-{other_owner[:8]}",
                password_hash="x",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()  # FK 强制：users 行先落库
        other_pdf = PdfFile(
            file_id=_uuid(),
            user_id=other_owner,  # 他人 PDF（原 device 域遗留种子——归属已切 user 域）
            filename="c.pdf",
            storage_key=_uuid(),
            size_bytes=1,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(other_pdf)
        session.flush()
        other_ch = Chapter(
            chapter_id=_uuid(), file_id=other_pdf.file_id, name="他章", start_page=1, end_page=2
        )
        session.add(other_ch)
        session.flush()
        foreign_id = other_ch.chapter_id
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=[ctx["chapter_ids"][0], foreign_id],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_tasks_get_missing_404(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_task(session, user_id=device, task_id=_uuid())
    assert excinfo.value.code is ErrorCode.TASK_NOT_FOUND


def test_tasks_cancel_keeps_cards(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        task_id = task.task_id
        # T8 起创建为 PENDING+PLANNING；"运行中取消" 前置由直写构造（RUNNING）
        task.status = "RUNNING"
        result = cancel_task(
            session, user_id=device, task_id=task_id, now="2026-08-11T01:00:00.000Z"
        )
        session.commit()
    assert result.status == "CANCELLED"


def test_tasks_resume_paused(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.flush()
        task.status = "PAUSED"
        task.resumable = 1
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = resume_task(
            session, user_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z"
        )
        session.commit()
    assert result.status == "RUNNING"
    # 再 resume（RUNNING 非 PAUSED）→ 409
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        resume_task(session, user_id=device, task_id=task_id, now="2026-08-11T02:00:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT


def test_tasks_resume_orphan_running_after_timeout(session_factory: Callable[[], Session]) -> None:
    """孤儿 RUNNING（updated_at 超 30 分钟）→ resume 抢占恢复（4.1）。"""
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.flush()
        # 模拟孤儿：RUNNING + updated_at 3 小时前（T8 起创建为 PENDING+PLANNING，
        # RUNNING 由规划 worker CAS 接管写入——本用例聚焦恢复状态机，直写 RUNNING）
        task.status = "RUNNING"
        task.updated_at = "2026-08-10T21:00:00.000Z"
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = resume_task(
            session, user_id=device, task_id=task_id, now="2026-08-11T00:30:00.000Z"
        )
        session.commit()
    assert result.status == "RUNNING"
    assert result.resumable == 0


def test_tasks_resume_running_fresh_conflicts(session_factory: Callable[[], Session]) -> None:
    """新鲜 RUNNING（心跳内）→ resume 409 TASK_STATE_CONFLICT。"""
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, user_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(
            session,
            user_id=device,
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config(),
            now="2026-08-11T00:00:00.000Z",
        )
        # T8 起创建为 PENDING+PLANNING；"新鲜 RUNNING" 前置由直写构造（心跳内）
        task.status = "RUNNING"
        session.commit()
        task_id = task.task_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        resume_task(session, user_id=device, task_id=task_id, now="2026-08-11T00:10:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
