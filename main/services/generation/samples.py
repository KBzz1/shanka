"""samples.py：样卡 service（6.3/AC-03）。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Chapter, PdfFile
from services.generation.fake import generate_card
from services.generation.validate import validate_config


def _owned_pdf(session: Session, *, device_id: str, file_id: str) -> PdfFile:
    pdf = session.get(PdfFile, file_id)
    if pdf is None or pdf.device_id != device_id:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "PDF 不存在")
    return pdf


def generate_samples(
    session: Session,
    *,
    device_id: str,
    file_id: str,
    chapter_ids: list[str],
    config: dict[str, Any],
) -> list[dict[str, object]]:
    validate_config(config)
    pdf = _owned_pdf(session, device_id=device_id, file_id=file_id)
    chapters = session.scalars(select(Chapter).where(Chapter.file_id == pdf.file_id)).all()
    by_id = {ch.chapter_id: ch for ch in chapters}
    missing = [cid for cid in chapter_ids if cid not in by_id]
    if missing:
        raise AppError(ErrorCode.PDF_NOT_FOUND, "章节不属于该 PDF")
    # 样卡：1 基础 + 1 理解 + 1 应用；2 问答 + 1 判断（fake 生成器按难度定类型）。
    # task_id 固定 "sample"：样卡不入库不参与去重（F-1 修复后 seed 带任务维度）
    first = by_id[chapter_ids[0]]
    return [
        generate_card(
            "样卡主题-基础", first.name, "BASIC", config.get("custom_requirements"), "sample"
        ),
        generate_card(
            "样卡主题-理解",
            first.name,
            "UNDERSTANDING",
            config.get("custom_requirements"),
            "sample",
        ),
        generate_card(
            "样卡主题-应用", first.name, "APPLICATION", config.get("custom_requirements"), "sample"
        ),
    ]
