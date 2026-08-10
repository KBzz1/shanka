"""KnowledgePoint 规划集成测试（3.5/5.4.1 可测口径）。

carry-forward（V1 教训）：engine 级 PRAGMA foreign_keys=ON（database-design 0），
pdf_files.device_id → devices 需 devices 行前置——fixture 先建设备
（HTTP 流中由 F1 设备中间件自动建立，本层显式补种）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from infra.db.models import Base, Chapter, Device, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from services.generation.planning import plan_knowledge_points


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'plan.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_device(session: Session, *, device_id: str) -> None:
    """devices 行前置（FK 强制，HTTP 流由 F1 设备中间件自动建立）。"""
    session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
    session.flush()


def _seed_chapters(session: Session, *, file_id: str, n: int = 2) -> list[str]:
    ids = []
    for i in range(n):
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=file_id,
            name=f"第{i + 1}章",
            start_page=i + 1,
            end_page=i + 2,
        )
        session.add(ch)
        session.flush()
        ids.append(ch.chapter_id)
    return ids


def _seed_pdf(session: Session, *, device_id: str) -> str:
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    return pdf.file_id


def test_planning_compact_le_balanced_le_extensive(
    session_factory: Callable[[], Session],
) -> None:
    """同章节同输入：COMPACT 知识点数 ≤ BALANCED ≤ EXTENSIVE（5.4.1 可测口径）。"""
    task_id = _uuid()
    with session_factory() as session:
        device_id = _uuid()
        _seed_device(session, device_id=device_id)
        file_id = _seed_pdf(session, device_id=device_id)
        chapter_ids = _seed_chapters(session, file_id=file_id, n=2)
        session.commit()
    counts: dict[str, int] = {}
    for tendency in ("COMPACT", "BALANCED", "EXTENSIVE"):
        with session_factory() as session:
            kps = plan_knowledge_points(
                session, task_id=task_id, chapter_ids=chapter_ids, quantity_tendency=tendency
            )
            counts[tendency] = len(kps)
            session.commit()
        assert counts[tendency] > 0
    assert counts["COMPACT"] <= counts["BALANCED"] <= counts["EXTENSIVE"]


def test_planning_knowledge_point_fields(session_factory: Callable[[], Session]) -> None:
    task_id = _uuid()
    with session_factory() as session:
        device_id = _uuid()
        _seed_device(session, device_id=device_id)
        file_id = _seed_pdf(session, device_id=device_id)
        chapter_ids = _seed_chapters(session, file_id=file_id, n=1)
        session.commit()
    with session_factory() as session:
        kps = plan_knowledge_points(
            session, task_id=task_id, chapter_ids=chapter_ids, quantity_tendency="BALANCED"
        )
        session.commit()
    for kp in kps:
        assert kp.task_id == task_id
        assert kp.source_chunk_id
        assert kp.topic
        assert kp.priority >= 1
        assert kp.status == "PENDING"
