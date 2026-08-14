"""样卡 service 集成测试：构成/不入库/校验（真实 SQLite）。

V1 教训 carry-forward：users FK 强制（PRAGMA foreign_keys=ON），service 层测试需
显式建立 users 行（HTTP 流由注册端点建立，见 test_pdf_service.py）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Card, Chapter, PdfFile, User
from infra.db.session import create_db_engine, create_session_factory
from services.generation.samples import generate_samples
from services.generation.validate import validate_config


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'samples.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_user(session: Session, user_id: str) -> None:
    """users 行先落库（FK 强制）：service 测试需显式建立。"""
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()


def _seed_pdf(session: Session, *, user_id: str) -> tuple[str, list[str]]:
    _ensure_user(session, user_id)
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=10,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    chapter_ids = []
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
    return pdf.file_id, chapter_ids


def _config(quantity: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        quantity_tendency=quantity,
        difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
    )


def test_samples_generates_three_not_persisted(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        file_id, chapter_ids = _seed_pdf(session, user_id=user)
        session.commit()
    with session_factory() as session:
        cards = generate_samples(
            session, user_id=user, file_id=file_id, chapter_ids=chapter_ids, config=_config()
        )
        session.commit()
    assert len(cards) == 3
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "UNDERSTANDING", "APPLICATION"}
    q_count = sum(1 for c in cards if c["card_type"] == "QUESTION")
    tf_count = sum(1 for c in cards if c["card_type"] == "TRUE_FALSE")
    assert q_count == 2 and tf_count == 1
    with session_factory() as session:
        assert session.scalar(select(Card).limit(1)) is None  # 不入库


def test_samples_cross_user_404(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        file_id, chapter_ids = _seed_pdf(session, user_id=user)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        generate_samples(
            session,
            user_id=_uuid(),
            file_id=file_id,
            chapter_ids=chapter_ids,
            config=_config(),
        )
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_samples_chapter_not_in_file_404(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        file_id, _ = _seed_pdf(session, user_id=user)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        generate_samples(
            session, user_id=user, file_id=file_id, chapter_ids=[_uuid()], config=_config()
        )
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_samples_validate_config() -> None:
    validate_config(_config())  # 合法
    with pytest.raises(AppError) as excinfo:
        validate_config(
            GenerationConfig(
                quantity_tendency="BALANCED",
                difficulty_ratio=DifficultyRatio(basic=0.5, understanding=0.5, application=0.2),
            )
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
    with pytest.raises(AppError) as excinfo:
        validate_config(
            GenerationConfig(
                quantity_tendency="HUGE",
                difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
            )
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR
