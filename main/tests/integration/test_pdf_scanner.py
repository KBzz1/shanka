"""services.pdf.scanner 集成测试：状态机/章节落库/失败分支/恢复 + V25-D-36 AI 章节规划分支。

V1 教训 carry-forward：user_id FK 强制（PRAGMA foreign_keys=ON），scanner 测试
需显式建立 users 行（见 test_pdf_service.py 同款 _ensure_user）。
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import (
    ApiKey,
    Base,
    Chapter,
    LearningProject,
    LlmCallAttempt,
    Material,
    PdfFile,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key
from infra.llm.deepseek import RetryableUpstreamError
from infra.storage.local import LocalStorage
from services.pdf.scanner import process_pending, scan_once, validate_upload

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")

_ENC_KEY = "00" * 32  # 64 位 hex = 32 字节 AES-256 密钥（测试用）


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, api_key_encryption_key=_ENC_KEY)  # type: ignore[call-arg]


def _ai_forced_settings() -> Settings:
    """强制走 AI 分支：阈值压 0（V25-D-38 分诊不再拦截小资料）。"""
    return Settings(  # type: ignore[call-arg]
        _env_file=None, api_key_encryption_key=_ENC_KEY, single_chapter_max_chars=0
    )


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scan.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_user(session: Session, user_id: str) -> None:
    """users 行先落库（FK 强制）：scanner 测试需显式建立。"""
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


def _save_key(session: Session, *, user_id: str) -> None:
    session.add(
        ApiKey(
            user_id=user_id,
            encrypted_key=encrypt_key("sk-test", bytes.fromhex(_ENC_KEY)),
            status="AVAILABLE",
            masked_key="sk-****test",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()


def _seed_pending(session: Session, *, user_id: str, storage_key: str) -> str:
    """V25-D-29 基座：PDF 行伴随 LearningProject + Material（scanner 按 material_id 写回章节）。"""
    _ensure_user(session, user_id)
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="扫描项目",
        version="2026-08-11T00:00:00.000Z",
        created_at="2026-08-11T00:00:00.000Z",
        updated_at="2026-08-11T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="book.pdf",
        storage_key=storage_key,
        size_bytes=100,
        status="PENDING",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    session.add(
        Material(
            material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project.project_id,
            type="PDF",
            name="book.pdf",
            status=None,
            size_bytes=100,
            created_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()
    return pdf.file_id


def test_scanner_process_pending_parses_sample(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(SAMPLE.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        project_id = session.scalar(
            select(Material.project_id).where(Material.material_id == file_id)
        )
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=settings)
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
        # 契约 4.5（V25-D-34）：解析终态发布必须刷新所属项目版本
        assert project_id is not None
        project = session.get(LearningProject, project_id)
        assert project is not None
        assert project.version != "2026-08-11T00:00:00.000Z"
        assert project.updated_at == project.version
    assert n == 1
    assert row is not None
    assert row.status == "PARSED"
    assert len(chapters) >= 3
    assert chapters[0].start_page is not None and chapters[0].start_page >= 1
    # 有目录 → source=TOC（V25-D-36），零 LLM 调用
    assert all(c.source == "TOC" for c in chapters)
    with session_factory() as session:
        assert (
            session.scalar(
                select(LlmCallAttempt.call_id).where(LlmCallAttempt.stage == "CHAPTER_PLANNING")
            )
            is None
        )


def test_scanner_process_pending_failed_keeps_file(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    """损坏 PDF → FAILED + error_code，原始文件保留；失败同样是终态跃迁，bump 项目版本。"""
    user = _uuid()
    with session_factory() as session:
        storage_key = storage.save(b"not a real pdf content")
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        project_id = session.scalar(
            select(Material.project_id).where(Material.material_id == file_id)
        )
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=settings)
        session.commit()
        row = session.get(PdfFile, file_id)
        assert project_id is not None
        project = session.get(LearningProject, project_id)
        assert project is not None
        assert project.version != "2026-08-11T00:00:00.000Z"  # 4.5
    assert n == 1
    assert row is not None
    assert row.status == "FAILED"
    assert row.error_code == "PDF_PARSE_FAILED"
    assert storage.open(row.storage_key).exists()  # 原始文件保留（5.1）


def test_scanner_scan_once_resumes_after_restart(
    session_factory: sessionmaker[Session], storage: LocalStorage, settings: Settings
) -> None:
    """重启恢复：PENDING/PARSING 残留重新入队处理。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = _uuid()
    with session_factory() as session:
        key1 = storage.save(SAMPLE.read_bytes())
        f1 = _seed_pending(session, user_id=user, storage_key=key1)
        # PARSING 残留（模拟崩溃）
        key2 = storage.save(SAMPLE.read_bytes())
        pdf2 = PdfFile(
            file_id=_uuid(),
            user_id=user,
            filename="b2.pdf",
            storage_key=key2,
            size_bytes=100,
            status="PARSING",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf2)
        session.flush()
        project2 = LearningProject(
            project_id=_uuid(),
            user_id=user,
            name="重启项目",
            version="2026-08-11T00:00:00.000Z",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(project2)
        session.flush()
        session.add(
            Material(
                material_id=pdf2.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=project2.project_id,
                type="PDF",
                name="b2.pdf",
                status=None,
                size_bytes=100,
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        f2 = pdf2.file_id
        session.commit()
    # 新 session/新 app（重启模拟）
    with session_factory() as session:
        n = scan_once(session_factory, storage=storage, settings=settings)
        assert n >= 2
    with session_factory() as session:
        row1 = session.get(PdfFile, f1)
        row2 = session.get(PdfFile, f2)
    assert row1 is not None and row2 is not None
    assert row1.status == "PARSED"
    assert row2.status == "PARSED"


def _write_text_page(path: Path, text: str = "hello world") -> None:
    """构造 1 页 PDF：content stream 手写文本 + Type1 Helvetica 资源，无 outline。

    构造法与 T1 解析器测试同款（pypdf 无 create_text API；add_blank_page 不产生
    文本层，需手写 content stream）——"有文本层无 outline"样本来源。
    """
    w = PdfWriter()
    page = w.add_blank_page(width=200, height=200)
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 160 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = w._add_object(content)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = w._add_object(font)
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
    )
    page[NameObject("/Resources")] = w._add_object(resources)
    with path.open("wb") as f:
        w.write(f)


class _StubClient:
    """章节规划 stub：按脚本逐次返回；记录调用以断言信封与页码锚定。"""

    def __init__(self, replies: list[str | Exception]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, str]] = []

    def chat(
        self,
        prompt: str,
        api_key: str = "",
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"user": prompt, "system": system_prompt or ""})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {
            "content": reply,
            "usage": {
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens": 5,
            },
            "model": "stub",
            "http_status": 200,
            "duration_ms": 1,
        }

    def close(self) -> None:
        return None


def test_scanner_no_toc_small_pdf_auto_single_chapter(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-38 分诊：小资料（≤阈值）无目录 → 直接单章（source=AUTO），零模型、
    不要求已存 Key（此前行为：FAILED + API_KEY_NOT_SET）。"""
    from infra.db.models import TextChunk

    user = _uuid()
    pdf_path = tmp_path / "notoc.pdf"
    _write_text_page(pdf_path)
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()  # 无 ApiKey 行
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=settings)
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
        chunks = session.scalars(select(TextChunk).where(TextChunk.material_id == file_id)).all()
        llm_rows = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.stage == "CHAPTER_PLANNING")
        ).all()
    assert n == 1
    assert row is not None and row.status == "PARSED"  # 无 Key 也成功
    assert len(chapters) == 1 and chapters[0].source == "AUTO"
    assert chapters[0].name == "book.pdf"  # 资料名
    assert chapters[0].start_page == 1 and chapters[0].end_page == 1
    assert len(chunks) == 1
    assert llm_rows == []  # 零模型调用


def test_scanner_no_toc_large_without_key_fails_not_set(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36 保留路径：超阈值无目录且未存 Key → FAILED + API_KEY_NOT_SET。"""
    from infra.db.models import TextChunk

    user = _uuid()
    pdf_path = tmp_path / "notoc-big.pdf"
    _write_text_page(pdf_path, text="x" * 2000)  # 1 页 2000 字符（PDF 文本流 ascii）
    forced = Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key_encryption_key=_ENC_KEY,
        single_chapter_max_chars=1000,  # 阈值 < 2000 → 强制超阈值
    )
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        session.commit()
    with session_factory() as session:
        n = process_pending(session, storage=storage, settings=forced)
        session.commit()
        row = session.get(PdfFile, file_id)
        chunks = session.scalars(select(TextChunk).where(TextChunk.material_id == file_id)).all()
    assert n == 1
    assert row is not None
    assert row.status == "FAILED"
    assert row.error_code == "API_KEY_NOT_SET"
    assert len(chunks) == 1  # 页文本先行落库


def test_scanner_no_toc_ai_plans_chapters(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36：无目录 + 已存 Key → AI 章节规划 → PARSED，章节 source=AI，
    账本 stage=CHAPTER_PLANNING 落库。"""
    user = _uuid()
    pdf_path = tmp_path / "notoc.pdf"
    _write_text_page(pdf_path)
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        _save_key(session, user_id=user)
        session.commit()
    stub = _StubClient([json.dumps({"chapters": [{"title": "第 1 章 绪论", "start_page": 1}]})])
    with session_factory() as session:
        n = process_pending(
            session, storage=storage, settings=_ai_forced_settings(), client_factory=lambda _k: stub
        )
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.stage == "CHAPTER_PLANNING")
        ).all()
    assert n == 1
    assert row is not None and row.status == "PARSED"
    assert len(chapters) == 1
    assert chapters[0].source == "AI"
    assert chapters[0].name == "第 1 章 绪论"
    assert chapters[0].start_page == 1 and chapters[0].end_page == 1
    assert len(attempts) == 1
    assert attempts[0].status == "SUCCESS"
    assert attempts[0].scope_type == "MATERIAL" and attempts[0].scope_id == file_id
    assert attempts[0].task_id is None
    assert attempts[0].operation_key == f"chapters:{file_id}:0"


def test_scanner_no_toc_ai_failure_fails_with_new_code(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36：AI 调用持续失败（预算耗尽）→ FAILED + PDF_AI_CHAPTERS_FAILED。"""
    user = _uuid()
    pdf_path = tmp_path / "notoc.pdf"
    _write_text_page(pdf_path)
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        _save_key(session, user_id=user)
        session.commit()
    errors: list[str | Exception] = [
        RetryableUpstreamError(ErrorCode.GENERATION_FAILED, "上游 5xx", retryable=True)
        for _ in range(3 + settings.ai_chapter_retry_limit)
    ]
    stub = _StubClient(errors)
    with session_factory() as session:
        n = process_pending(
            session, storage=storage, settings=_ai_forced_settings(), client_factory=lambda _k: stub
        )
        session.commit()
        row = session.get(PdfFile, file_id)
    assert n == 1
    assert row is not None
    assert row.status == "FAILED"
    assert row.error_code == "PDF_AI_CHAPTERS_FAILED"


def test_scanner_no_toc_zero_boundaries_degrades_to_whole_book(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36：AI 全程（含 0 边界引导重试）成功仍 0 有效边界 → 静默降级整本单章
    （source=AI），不 FAILED；账本记录 :guided 独立轮次。"""
    user = _uuid()
    pdf_path = tmp_path / "notoc.pdf"
    _write_text_page(pdf_path)
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        _save_key(session, user_id=user)
        session.commit()
    stub = _StubClient([json.dumps({"chapters": []}), json.dumps({"chapters": []})])
    with session_factory() as session:
        n = process_pending(
            session, storage=storage, settings=_ai_forced_settings(), client_factory=lambda _k: stub
        )
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
        keys = session.scalars(
            select(LlmCallAttempt.operation_key).where(LlmCallAttempt.stage == "CHAPTER_PLANNING")
        ).all()
    assert n == 1
    assert row is not None and row.status == "PARSED"
    assert len(chapters) == 1
    assert chapters[0].source == "AI"
    assert chapters[0].name == "book.pdf"  # 资料名
    assert chapters[0].start_page == 1 and chapters[0].end_page == 1
    assert sorted(keys) == [f"chapters:{file_id}:0", f"chapters:{file_id}:0:guided"]
    assert len(stub.calls) == 2
    assert ":guided" not in stub.calls[0]["user"]  # 首次无引导指令
    assert "章节体系判定原理" in stub.calls[1]["user"]  # 引导重试附加指令


def test_scanner_no_toc_guided_retry_recovers_boundaries(
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    storage: LocalStorage,
    settings: Settings,
) -> None:
    """V25-D-36 引导重试恢复：首报 0 边界 → 引导轮识别出边界 → 采用并 PARSED，
    不再整本降级。"""
    user = _uuid()
    pdf_path = tmp_path / "notoc.pdf"
    _write_text_page(pdf_path)
    with session_factory() as session:
        storage_key = storage.save(pdf_path.read_bytes())
        file_id = _seed_pending(session, user_id=user, storage_key=storage_key)
        _save_key(session, user_id=user)
        session.commit()
    stub = _StubClient(
        [
            json.dumps({"chapters": []}),
            json.dumps({"chapters": [{"title": "一 开场题", "start_page": 1}]}),
        ]
    )
    with session_factory() as session:
        n = process_pending(
            session, storage=storage, settings=_ai_forced_settings(), client_factory=lambda _k: stub
        )
        session.commit()
        row = session.get(PdfFile, file_id)
        chapters = session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all()
    assert n == 1
    assert row is not None and row.status == "PARSED"
    assert len(chapters) == 1
    assert chapters[0].source == "AI"
    assert chapters[0].name == "一 开场题"
    assert chapters[0].start_page == 1 and chapters[0].end_page == 1


def test_scanner_validate_upload_page_count_boundary() -> None:
    """页数维度边界（T3 审查补覆盖）：=1000 通过；1001 → PDF_UPLOAD_INVALID；None 跳过。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # 默认值断言：不受仓库根 .env 加载影响
    validate_upload(
        filename="a.pdf",
        content_type="application/pdf",
        magic=b"%PDF-1.4",
        size_bytes=100,
        page_count_hint=1000,
        settings=settings,
    )
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="application/pdf",
            magic=b"%PDF-1.4",
            size_bytes=100,
            page_count_hint=1001,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # None → 跳过页数校验（hint 不可得时由扫描器兜底）
    validate_upload(
        filename="a.pdf",
        content_type="application/pdf",
        magic=b"%PDF-1.4",
        size_bytes=100,
        page_count_hint=None,
        settings=settings,
    )


def test_scanner_validate_upload_size_boundary_exact_max() -> None:
    """大小边界（T3 审查补覆盖）：==上限（100MB）通过（超限已由 triple_check 覆盖）。"""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # 默认值断言：不受仓库根 .env 加载影响
    validate_upload(
        filename="a.pdf",
        content_type="application/pdf",
        magic=b"%PDF-1.4",
        size_bytes=settings.pdf_max_size_bytes,
        page_count_hint=None,
        settings=settings,
    )


def test_scanner_validate_upload_triple_check(tmp_path: Path) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # 默认值断言：不受仓库根 .env 加载影响
    # 合法
    validate_upload(
        filename="a.pdf",
        content_type="application/pdf",
        magic=b"%PDF-1.4",
        size_bytes=100,
        page_count_hint=None,
        settings=settings,
    )
    # 扩展名
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.txt",
            content_type="application/pdf",
            magic=b"%PDF-1.4",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # 魔数
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="application/pdf",
            magic=b"not-pdf",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # MIME
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="text/plain",
            magic=b"%PDF-1.4",
            size_bytes=100,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
    # 大小
    with pytest.raises(AppError) as excinfo:
        validate_upload(
            filename="a.pdf",
            content_type="application/pdf",
            magic=b"%PDF-1.4",
            size_bytes=101 * 1024 * 1024,
            page_count_hint=None,
            settings=settings,
        )
    assert excinfo.value.code is ErrorCode.PDF_UPLOAD_INVALID
