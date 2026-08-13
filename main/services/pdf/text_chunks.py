"""services.pdf.text_chunks：页文本持久化（spec §4.1，一页一行、与章节解耦）。

- `chunk_id` 由服务端按 `(file_id, page_number, content_sha256)` 确定性生成
  （uuid5），同一 PDF 内容的页标识稳定；
- 重解析先清理该 file_id 既有页文本再重建（幂等）；
- 页文本不随章节编辑/删除重建；PDF 删除按 file_id 级联清理（FK ON DELETE
  CASCADE）。
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import TextChunk
from services.pdf.parser import PageText


def chunk_id_for(file_id: str, page_number: int, content: str) -> str:
    """页标识确定性生成：`uuid5(NAMESPACE_URL, "{file_id}:{page_number}:{sha256(content)}")`。

    同 (file_id, page_number, content) 恒同；内容变化即换 ID。
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{file_id}:{page_number}:{digest}"))


def persist_text_chunks(session: Session, *, file_id: str, pages: list[PageText], now: str) -> None:
    """重建 file_id 的页文本：先删该 file_id 既有再插（重解析幂等，同事务）。"""
    for old in session.scalars(select(TextChunk).where(TextChunk.file_id == file_id)).all():
        session.delete(old)
    session.flush()
    for page in pages:
        content = page["content"]
        session.add(
            TextChunk(
                chunk_id=chunk_id_for(file_id, page["page_number"], content),
                file_id=file_id,
                page_number=page["page_number"],
                char_count=len(content),
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                content=content,
                created_at=now,
            )
        )


def load_pages(
    session: Session, *, file_id: str, start_page: int, end_page: int
) -> list[TextChunk]:
    """按 page_number 升序读取闭区间 [start_page, end_page] 的页文本。"""
    return list(
        session.scalars(
            select(TextChunk)
            .where(
                TextChunk.file_id == file_id,
                TextChunk.page_number >= start_page,
                TextChunk.page_number <= end_page,
            )
            .order_by(TextChunk.page_number)
        ).all()
    )


def page_text_map(chunks: list[TextChunk]) -> dict[int, str]:
    """页文本映射 {page_number: content}（generator 输入形状）。"""
    return {chunk.page_number: chunk.content for chunk in chunks}
