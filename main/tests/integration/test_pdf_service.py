"""services.pdf.service 集成测试：上传/列表/详情/删除/章节（真实 SQLite + 临时存储；user 域）。

V1 教训 carry-forward：users FK 强制（PRAGMA foreign_keys=ON），service 层测试需
显式建立 users 行（HTTP 流由注册端点建立，见 test_decks_service.py）。
"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Chapter, LearningProject, Material, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from infra.storage.local import LocalStorage
from services.pdf.service import (
    delete_pdf,
    get_pdf,
    list_pdfs,
    update_chapter,
    upload_pdf,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pdf.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


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


def _seed_pdf(
    session: Session,
    *,
    user_id: str,
    storage_key: str = "",
    status: str = "PARSED",
    with_project: bool = True,
) -> str:
    """V25-D-29 基座：PDF 行伴随 LearningProject + Material（material_id == file_id）。

    with_project=False 种孤儿 PDF（迁移前遗留形态，delete_pdf 走旧语义分支）。
    """
    _ensure_user(session, user_id)
    project_id = _uuid()
    if with_project:
        session.add(
            LearningProject(
                project_id=project_id,
                user_id=user_id,
                name="种子项目",
                version="2026-08-11T00:00:00.000Z",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="book.pdf",
        storage_key=storage_key or _uuid(),
        size_bytes=100,
        status=status,
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    if with_project:
        session.add(
            Material(
                material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=project_id,
                type="PDF",
                name="book.pdf",
                status=None,
                size_bytes=100,
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
    return pdf.file_id


def test_pdf_service_upload_creates_pending(
    session_factory: Callable[[], Session], storage: LocalStorage
) -> None:
    user = _uuid()
    with session_factory() as session:
        _ensure_user(session, user)
        pdf = upload_pdf(
            session,
            user_id=user,
            filename="book.pdf",
            size_bytes=100,
            storage_key=_uuid(),
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        file_id = pdf.file_id
    assert pdf.status == "PENDING"
    with session_factory() as session:
        row = session.get(PdfFile, file_id)
        assert row is not None
        assert row.user_id == user


def test_pdf_service_list_isolated_and_sorted(session_factory: Callable[[], Session]) -> None:
    user_a, user_b = _uuid(), _uuid()
    with session_factory() as session:
        _seed_pdf(session, user_id=user_a)
        _seed_pdf(session, user_id=user_b)
        session.commit()
    with session_factory() as session:
        list_a = list_pdfs(session, user_id=user_a)
        list_b = list_pdfs(session, user_id=user_b)
    assert len(list_a) == 1 and len(list_b) == 1


def test_pdf_service_get_cross_user_404(session_factory: Callable[[], Session]) -> None:
    user_a, user_b = _uuid(), _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, user_id=user_a)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        get_pdf(session, user_id=user_b, file_id=file_id)
    assert excinfo.value.code is ErrorCode.PDF_NOT_FOUND


def test_pdf_service_delete_removes_and_cleans_storage(
    session_factory: Callable[[], Session], storage: LocalStorage, tmp_path: Path
) -> None:
    user = _uuid()
    storage_key = uuid.uuid4().hex  # storage.open 严格校验 32 位 hex（草稿瑕疵修正）
    with session_factory() as session:
        file_id = _seed_pdf(session, user_id=user, storage_key=storage_key)
        # 终态任务引用：删除后应保留任务、file_id SET NULL（database-design §3）
        task = Task(
            task_id=_uuid(),
            user_id=user,
            file_id=file_id,
            status="COMPLETED",
            selected_chapters="[]",
            generation_config="{}",
            generated_card_count=0,
            resumable=0,
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(task)
        session.commit()
        task_id = task.task_id
    # 写存储对象（模拟扫描器/上传产物；storage.open 前需先建父目录）
    obj_path = storage.open(storage_key)
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(b"%PDF-1.4 fake")
    assert obj_path.exists()
    with session_factory() as session:
        delete_pdf(session, user_id=user, file_id=file_id, storage=storage)
        session.commit()
    with session_factory() as session:
        assert session.get(PdfFile, file_id) is None
        task_row = session.get(Task, task_id)
        assert task_row is not None  # 终态任务保留
        assert task_row.file_id is None  # SET NULL
    assert not obj_path.exists()  # 存储清理


def test_pdf_service_delete_auto_cancels_non_terminal_task(
    session_factory: Callable[[], Session], storage: LocalStorage
) -> None:
    user = _uuid()
    task_id = _uuid()
    with session_factory() as session:
        # 孤儿 PDF（无项目/资料行）→ delete_pdf 旧语义分支：取消活跃任务 + SET NULL
        file_id = _seed_pdf(session, user_id=user, with_project=False)
        session.add(
            Task(
                task_id=task_id,
                user_id=user,
                file_id=file_id,
                status="GENERATING",
                selected_chapters="[]",
                generation_config="{}",
                generated_card_count=0,
                resumable=0,
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
    # 契约 570：孤儿 PDF 删除同样自动取消活跃任务（与项目/牌组删除同一围栏语义）
    with session_factory() as session:
        delete_pdf(session, user_id=user, file_id=file_id, storage=storage)
        session.commit()
    with session_factory() as session:
        assert session.get(PdfFile, file_id) is None
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "ABANDONED"
        assert task.file_id is None


def test_pdf_service_update_chapter(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, user_id=user)
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=file_id,
            material_id=file_id,
            name="旧名",
            start_page=1,
            end_page=10,
        )
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session:
        updated = update_chapter(
            session,
            user_id=user,
            file_id=file_id,
            chapter_id=chapter_id,
            name="新名",
            start_page=2,
            end_page=8,
            now="2026-08-11T01:00:00.000Z",
        )
        session.commit()
    assert updated.name == "新名"
    assert updated.start_page == 2 and updated.end_page == 8


def test_pdf_service_update_chapter_invalid_range(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, user_id=user)
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=file_id,
            material_id=file_id,
            name="c",
            start_page=1,
            end_page=10,
        )
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        update_chapter(
            session,
            user_id=user,
            file_id=file_id,
            chapter_id=chapter_id,
            name="x",
            start_page=5,
            end_page=3,
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.VALIDATION_ERROR


def test_pdf_service_update_chapter_not_parsed(session_factory: Callable[[], Session]) -> None:
    """非 PARSED 时 PATCH 章节 → 409 TASK_STATE_CONFLICT（裁决）。"""
    user = _uuid()
    with session_factory() as session:
        file_id = _seed_pdf(session, user_id=user, status="FAILED")
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=file_id,
            material_id=file_id,
            name="c",
            start_page=1,
            end_page=5,
        )
        session.add(ch)
        session.commit()
        chapter_id = ch.chapter_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        update_chapter(
            session,
            user_id=user,
            file_id=file_id,
            chapter_id=chapter_id,
            name="x",
            start_page=1,
            end_page=5,
            now="2026-08-11T01:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
