"""services.pdf.scanner：进程内 DB 驱动 PDF 解析扫描器（契约 4.4 定式）。

状态机：PENDING → PARSING(短租约) → PARSED / FAILED(error_code)。
- 领取阶段只持有短数据库事务；解析耗时在事务外进行，租约过期后可被另一 worker 接管；
- 删除/替换会递增 parse_version 并清除租约，迟到结果通过 token/version 栅栏丢弃；
- 重复解析幂等：发布前清理该 file_id 的既有 chapters 与 text_chunks 再重建；
- PARSED 时完整页文本一页一行落 text_chunks（spec §4.1，与章节解耦）；
- 失败不删除原始文件（5.1）；FAILED 行不再重试（终态；V25-D-36 的 AI 失败码可经
  reparse 端点重置 PENDING 重走本扫描器）。

V25-D-36 无目录分支：parse_pdf 返回 chapters=None（不再抛 PDF_TOC_MISSING）→
页文本先行落库后，以资料归属用户的 API Key 调 services/chapters AI 章节规划
（source=AI，多段 LLM 调用耗时可超租约——段间条件 UPDATE 刷新 parse_lease_until，
栅栏语义不变）；无 Key → FAILED + API_KEY_NOT_SET，规划失败 → FAILED +
PDF_AI_CHAPTERS_FAILED（text_chunks 已落，whole-book 降级与 reparse 不需重解析文件）。
"""

import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import ApiKey, Chapter, LearningProject, Material, PdfFile
from infra.db.session import format_utc
from infra.llm.crypto import decrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient, LlmChatClient
from services.chapters.planner import LeaseLost, plan_chapters
from services.chapters.triage import is_single_chapter_by_size
from services.pdf.parser import ChapterInfo, extract_pages, parse_pdf
from services.pdf.text_chunks import load_pages, persist_text_chunks
from services.projects.versioning import bump_project_version

logger = logging.getLogger(__name__)

_LEASE_MINUTES = 10


def _bump_owner_project(session: Session, *, file_id: str, now: str) -> None:
    """解析终态发布后刷新所属项目版本（契约 4.5）；无所属 material 行则跳过。"""
    project_id = session.scalar(select(Material.project_id).where(Material.material_id == file_id))
    if project_id is not None:
        bump_project_version(session, project_id=project_id, now=now)


def _fence_ok(session: Session, *, file_id: str, lease_token: str, parse_version: int) -> bool:
    """栅栏复检（列投影直发 SQL，绕开 expire_on_commit=False 的 identity map 缓存）。"""
    row = session.execute(
        select(PdfFile.status, PdfFile.parse_lease_token, PdfFile.parse_version).where(
            PdfFile.file_id == file_id
        )
    ).first()
    return (
        row is not None
        and row.status == "PARSING"
        and row.parse_lease_token == lease_token
        and int(row.parse_version) == parse_version
    )


def _renew_lease(session: Session, *, file_id: str, lease_token: str, parse_version: int) -> bool:
    """AI 规划段间租约刷新：条件 UPDATE 延长 parse_lease_until；rowcount=0 = 栅栏失效。"""
    lease_until = format_utc(SystemClock().now_utc() + timedelta(minutes=_LEASE_MINUTES))
    renewed = cast(
        CursorResult[Any],
        session.execute(
            update(PdfFile)
            .where(
                PdfFile.file_id == file_id,
                PdfFile.status == "PARSING",
                PdfFile.parse_lease_token == lease_token,
                PdfFile.parse_version == parse_version,
            )
            .values(parse_lease_until=lease_until)
        ),
    )
    session.commit()
    return renewed.rowcount == 1


def _decrypt_api_key_for_user(session: Session, *, user_id: str, settings: Settings) -> str:
    """从 api_keys 表取该用户 encrypted_key 解密（executor 同款定式；红线 4）。

    未保存可用 Key → API_KEY_NOT_SET（用户可配置后 reparse，不重传文件）；
    服务端加密配置缺失/解密失败 → API_KEY_UNAVAILABLE。
    """
    encrypted = session.scalar(
        select(ApiKey.encrypted_key).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
    )
    if encrypted is None:
        raise AppError(ErrorCode.API_KEY_NOT_SET, "尚未保存可用的 API Key，请配置后重试解析")
    key = key_from_settings(settings)
    if key is None:
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 不可用（加密配置缺失）")
    try:
        return decrypt_key(encrypted, key)
    except Exception:  # noqa: BLE001 —— 解密失败统一 API_KEY_UNAVAILABLE
        raise AppError(ErrorCode.API_KEY_UNAVAILABLE, "API Key 解密失败") from None


def _ai_plan_chapters(
    session: Session,
    *,
    file_id: str,
    settings: Settings,
    client_factory: Callable[[str], LlmChatClient] | None,
    lease_token: str,
    parse_version: int,
) -> list[ChapterInfo]:
    """无目录资料的 AI 章节规划分支（V25-D-36）：解 Key → 构造 client → 分段规划。"""
    material = session.get(Material, file_id)
    if material is None or material.project_id is None:
        raise AppError(ErrorCode.PDF_AI_CHAPTERS_FAILED, "资料归属缺失，无法规划章节")
    user_id = session.scalar(
        select(LearningProject.user_id).where(LearningProject.project_id == material.project_id)
    )
    if user_id is None:
        raise AppError(ErrorCode.PDF_AI_CHAPTERS_FAILED, "资料归属用户缺失，无法规划章节")
    api_key = _decrypt_api_key_for_user(session, user_id=user_id, settings=settings)
    client = (
        client_factory(api_key)
        if client_factory is not None
        else DeepSeekClient(settings, api_key=api_key)
    )
    chunks = load_pages(session, material_id=file_id)
    plans = plan_chapters(
        session,
        user_id=user_id,
        material_id=file_id,
        material_name=material.name,
        chunks=chunks,
        client=client,
        settings=settings,
        on_segment_done=lambda: _renew_lease(
            session, file_id=file_id, lease_token=lease_token, parse_version=parse_version
        ),
    )
    return [
        ChapterInfo(name=p["name"], start_page=p["start_page"], end_page=p["end_page"])
        for p in plans
    ]


def validate_upload(
    *,
    filename: str,
    content_type: str,
    magic: bytes,
    size_bytes: int,
    page_count_hint: int | None,
    settings: Settings,
) -> None:
    """三重校验 + 限制（6.1）：魔数/扩展名/MIME + ≤50MB + ≤500 页。

    失败 message 细分到具体条件（2026-08-11 联调诊断：区分 400 的具体原因，
    前端按 message/localization_key 提示，日志侧由请求日志 error_code 记录）。
    """
    ok_ext = filename.lower().endswith(".pdf")
    ok_magic = magic.startswith(b"%PDF")
    ok_mime = content_type.lower() == "application/pdf"
    ok_size = size_bytes <= settings.pdf_max_size_bytes
    ok_pages = page_count_hint is None or page_count_hint <= settings.pdf_max_pages
    if not (ok_ext and ok_magic and ok_mime and ok_size and ok_pages):
        reasons = []
        if not ok_ext:
            reasons.append(f"扩展名非 .pdf（{filename!r}）")
        if not ok_magic:
            reasons.append("文件头非 %PDF")
        if not ok_mime:
            reasons.append(f"MIME 非 application/pdf（{content_type!r}）")
        if not ok_size:
            reasons.append(
                f"超过 {settings.pdf_max_size_bytes // (1024 * 1024)}MB 限制（{size_bytes} bytes）"
            )
        if not ok_pages:
            reasons.append(f"超过 {settings.pdf_max_pages} 页限制")
        raise AppError(ErrorCode.PDF_UPLOAD_INVALID, "PDF 文件校验失败：" + "；".join(reasons))


def process_pending(
    session: Session,
    *,
    storage: Any,
    settings: Settings,
    client_factory: Callable[[str], LlmChatClient] | None = None,
) -> int:
    """领取并处理一条 PDF；解析耗时阶段不持有数据库事务。

    ``parse_lease_token`` 和 ``parse_version`` 组成发布栅栏：删除/替换 PDF 后，旧扫描器
    即便已经读完文件，也只能丢弃结果，不能把章节或文本块写回新状态。V25-D-36：
    无目录资料在页文本先行落库后走 AI 章节规划（多段 LLM 调用，段间刷新租约）。
    """
    now_dt = SystemClock().now_utc()
    now = format_utc(now_dt)
    lease_until = format_utc(now_dt + timedelta(minutes=_LEASE_MINUTES))
    row = session.scalar(
        select(PdfFile)
        .where(
            PdfFile.status.in_(["PENDING", "PARSING"]),
            (PdfFile.parse_lease_until.is_(None) | (PdfFile.parse_lease_until <= now)),
        )
        .order_by(PdfFile.created_at)
        .limit(1)
    )
    if row is None:
        return 0
    file_id = row.file_id
    storage_key = row.storage_key
    lease_token = str(uuid.uuid4())
    parse_version = int(row.parse_version) + 1
    # The initial read only chooses a candidate.  Claim it with a conditional UPDATE so two
    # workers that observe the same expired lease cannot both parse/publish the file.
    claimed = cast(
        CursorResult[Any],
        session.execute(
            update(PdfFile)
            .where(
                PdfFile.file_id == file_id,
                PdfFile.status.in_(["PENDING", "PARSING"]),
                (PdfFile.parse_lease_until.is_(None) | (PdfFile.parse_lease_until <= now)),
            )
            .values(
                status="PARSING",
                parse_lease_token=lease_token,
                parse_lease_until=lease_until,
                parse_version=PdfFile.parse_version + 1,
            )
        ),
    )
    if claimed.rowcount != 1:
        session.rollback()
        return 0
    # 先提交领取，释放写事务，让项目删除可以在解析期间完成。
    session.commit()
    try:
        path = storage.open(storage_key)
        _text_sample, outline_chapters = parse_pdf(path)
        pages = extract_pages(path)
        # V25-D-36：页文本先行落库（栅栏复检后独立提交）——AI 规划可能耗时数分钟，
        # text_chunks 先行使 FAILED 后的 reparse / whole-book 降级无需重新解析文件；
        # 重解析幂等（先删后建）语义不变，栅栏失效时由接管方/删除方级联清理。
        if not _fence_ok(
            session, file_id=file_id, lease_token=lease_token, parse_version=parse_version
        ):
            session.rollback()
            logger.info("pdf parse result discarded", extra={"file_id": file_id})
            return 1
        persist_text_chunks(session, file_id=file_id, pages=pages, now=now)
        session.commit()

        source = "TOC"
        chapters: list[ChapterInfo]
        if outline_chapters is not None:
            chapters = outline_chapters
        elif is_single_chapter_by_size(sum(len(page["content"]) for page in pages), settings):
            # V25-D-38 确定性分诊：无目录但总字符 ≤ 阈值 → 直接单章（零模型、不要求
            # 已存 Key）；名称用资料名，区间 1..总页数
            material_name = session.scalar(
                select(Material.name).where(Material.material_id == file_id)
            )
            chapters = [
                ChapterInfo(
                    name=material_name or "全文",
                    start_page=1,
                    end_page=len(pages),
                )
            ]
            source = "AUTO"
        else:
            # 无目录且超阈值 → AI 章节规划兜底（V25-D-36）；租约丢失 = 并发接管/删除，静默丢弃
            try:
                chapters = _ai_plan_chapters(
                    session,
                    file_id=file_id,
                    settings=settings,
                    client_factory=client_factory,
                    lease_token=lease_token,
                    parse_version=parse_version,
                )
            except LeaseLost:
                session.rollback()
                logger.info("pdf ai chapter planning lease lost", extra={"file_id": file_id})
                return 1
            source = "AI"

        if not _fence_ok(
            session, file_id=file_id, lease_token=lease_token, parse_version=parse_version
        ):
            session.rollback()
            logger.info("pdf parse result discarded", extra={"file_id": file_id})
            return 1
        for old in session.scalars(select(Chapter).where(Chapter.file_id == file_id)).all():
            session.delete(old)
        session.flush()
        for ch in chapters:
            session.add(
                Chapter(
                    chapter_id=str(uuid.uuid4()),
                    file_id=file_id,
                    material_id=file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                    name=ch["name"],
                    source=source,
                    start_page=ch["start_page"],
                    end_page=ch["end_page"],
                )
            )
        # Publish through a conditional UPDATE, not only an in-memory object assignment.  A
        # project deletion/replacement may have committed after the pre-publish read; the fence
        # then affects zero rows and all parsed chunks/chapters are rolled back as stale output.
        published = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="PARSED",
                    error_code=None,
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if published.rowcount != 1:
            session.rollback()
            logger.info("pdf parse result discarded", extra={"file_id": file_id})
            return 1
        _bump_owner_project(session, file_id=file_id, now=now)
    except AppError as exc:
        logger.warning(
            "pdf parse failed",
            extra={"error_code": exc.code.value, "error_message": str(exc), "file_id": file_id},
        )
        session.rollback()
        failed = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="FAILED",
                    error_code=exc.code.value,
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if failed.rowcount != 1:
            session.rollback()
        else:
            _bump_owner_project(session, file_id=file_id, now=now)
    except Exception:  # noqa: BLE001
        logger.warning("pdf parse unexpected failure", extra={"error_code": "PDF_PARSE_FAILED"})
        session.rollback()
        failed = cast(
            CursorResult[Any],
            session.execute(
                update(PdfFile)
                .where(
                    PdfFile.file_id == file_id,
                    PdfFile.status == "PARSING",
                    PdfFile.parse_lease_token == lease_token,
                    PdfFile.parse_version == parse_version,
                )
                .values(
                    status="FAILED",
                    error_code="PDF_PARSE_FAILED",
                    parse_lease_token=None,
                    parse_lease_until=None,
                )
            ),
        )
        if failed.rowcount != 1:
            session.rollback()
        else:
            _bump_owner_project(session, file_id=file_id, now=now)
    return 1


def scan_once(
    session_factory: sessionmaker[Session],
    *,
    storage: Any,
    settings: Settings,
    client_factory: Callable[[str], LlmChatClient] | None = None,
) -> int:
    """扫描一轮：处理全部可解析行（MVP 逐条）。返回处理数。"""
    total = 0
    with session_factory() as session:
        while True:
            n = process_pending(
                session, storage=storage, settings=settings, client_factory=client_factory
            )
            if n == 0:
                break
            session.commit()
            total += n
    return total
