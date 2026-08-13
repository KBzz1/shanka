"""任务创建改造测试（spec §6.1/§10；Task 8）：PENDING+PLANNING 创建快照 + 预算硬上限。

- 基座同 test_tasks_service.py：真实 SQLite（PRAGMA foreign_keys=ON）全表建库，
  devices 前置 + PDF/章节/牌组 + ApiKey 种子；
- brief 中 `settings_override` fixture 在仓库不存在（T7 先例同样 adaptation），按
  仓库约定显式构造 Settings：预算测试走 executor 定式 `session.info["settings"]`
  注入通道，边界测试走 create_task 显式参数通道（adaptation 见任务报告）；
- 既有 test_tasks_service.py::test_tasks_create_runs_and_plans 的 RUNNING+同事务
  规划断言属旧创建语义，T16 更新；本文件只测新语义。
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import ApiKey, Base, Chapter, Device, KnowledgePoint, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.tasks.service import create_task, task_view

_NOW = "2026-08-12T00:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'create.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed(session: Session, *, user_id: str, chapter_count: int = 2) -> dict[str, Any]:
    """users 前置 + PDF + chapter_count 章节 + 牌组 + ApiKey 种子（tasks 校验 Key）。

    ApiKey/Device 保持 device 域种子（Task 5 前），键值同 user_id（过渡）。
    """
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
    session.add(Device(device_id=user_id, created_at=_NOW))
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
    deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
    session.flush()
    chapter_ids: list[str] = []
    chapters: list[dict[str, object]] = []
    for i in range(chapter_count):
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
        chapters.append(
            {
                "chapter_id": ch.chapter_id,
                "name": ch.name,
                "start_page": ch.start_page,
                "end_page": ch.end_page,
            }
        )
    session.add(
        ApiKey(
            device_id=user_id,
            encrypted_key="enc",
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    return {
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
        "chapters": chapters,
    }


def _config(tendency: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        quantity_tendency=tendency,
        difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
    )


def _budget_settings(tmp_path: Path, *, max_units: int) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'budget.db'}",
        storage_path=tmp_path / "storage",
        max_generation_units_per_task=max_units,
    )


def test_create_task_pending_planning(session: Session) -> None:
    """创建即 PENDING+PLANNING：started_at/total_batch_count 为空、快照完整、不再同事务规划。"""
    user = _uuid()
    ctx = _seed(session, user_id=user)
    task = create_task(
        session,
        user_id=user,
        device_id=user,  # 双头过渡：ApiKey 校验仍 device 域
        file_id=ctx["file_id"],
        deck_id=ctx["deck_id"],
        chapter_ids=ctx["chapter_ids"],
        config=_config(),
        now=_NOW,
    )
    assert task.status == "PENDING"
    assert task.stage == "PLANNING"
    assert task.started_at is None
    assert task.total_batch_count is None
    # 创建快照语义（契约 3.4/3.6）：完整 Chapter 对象 JSON，与现状一致
    assert json.loads(task.selected_chapters) == ctx["chapters"]
    # 不再同事务规划：无 KnowledgePoint 落库（T9 规划 worker CAS 接管后落库）
    kps = session.scalars(
        select(KnowledgePoint).where(KnowledgePoint.task_id == task.task_id)
    ).all()
    assert kps == []
    # task_view 新字段（T1 列）：completion_reason / skipped_planning_group_count
    view = task_view(task)
    assert view["completion_reason"] is None
    assert view["skipped_planning_group_count"] == 0


def test_create_task_budget_exceeded_rejected(session: Session, tmp_path: Path) -> None:
    """预算硬上限（spec §10）：5 章 COMPACT=15 单元 > 5 → VALIDATION_ERROR，不创建任务。"""
    user = _uuid()
    ctx = _seed(session, user_id=user, chapter_count=5)
    session.info["settings"] = _budget_settings(tmp_path, max_units=5)  # executor 定式注入
    with pytest.raises(AppError) as excinfo:
        create_task(
            session,
            user_id=user,
            device_id=user,  # 双头过渡：ApiKey 校验仍 device 域
            file_id=ctx["file_id"],
            deck_id=ctx["deck_id"],
            chapter_ids=ctx["chapter_ids"],
            config=_config("COMPACT"),
            now=_NOW,
        )
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert excinfo.value.message == "生成单元预算超出上限"
    assert session.scalars(select(Task)).all() == []  # 不创建任务


def test_create_task_budget_boundary_accepted(session: Session, tmp_path: Path) -> None:
    """预算等于上限（5 章 COMPACT=15 = 15）→ 正常创建 PENDING（显式 settings 参数通道）。"""
    user = _uuid()
    ctx = _seed(session, user_id=user, chapter_count=5)
    task = create_task(
        session,
        user_id=user,
        device_id=user,  # 双头过渡：ApiKey 校验仍 device 域
        file_id=ctx["file_id"],
        deck_id=ctx["deck_id"],
        chapter_ids=ctx["chapter_ids"],
        config=_config("COMPACT"),
        now=_NOW,
        settings=_budget_settings(tmp_path, max_units=15),
    )
    assert task.status == "PENDING"
    assert task.stage == "PLANNING"
