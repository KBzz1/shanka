"""services.pdf.text_chunks：页文本持久化（spec §4.1，一页一行、与章节解耦）。

- `chunk_id` 由服务端按 `(file_id, page_number, content_sha256)` 确定性生成
  （uuid5），同一 PDF 内容的页标识稳定；
- 重解析先清理该 file_id 既有页文本再重建（幂等）；
- 页文本不随章节编辑/删除重建；PDF 删除按 file_id 级联清理（FK ON DELETE
  CASCADE）。
"""

import hashlib
import re
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


def persist_text_chunks(
    session: Session,
    *,
    file_id: str,
    material_id: str | None = None,
    pages: list[PageText],
    now: str,
) -> None:
    """重建 file_id/material_id 的页文本：先删既有再插（重解析幂等，同事务）。

    PDF 资料 material_id == file_id（省缺省由 file_id 推导）。
    """
    mid = material_id or file_id
    for old in session.scalars(select(TextChunk).where(TextChunk.material_id == mid)).all():
        session.delete(old)
    session.flush()
    for page in pages:
        content = page["content"]
        seq = int(page["page_number"])
        session.add(
            TextChunk(
                chunk_id=chunk_id_for(file_id, page["page_number"], content),
                file_id=file_id,
                material_id=mid,
                chunk_seq=seq,
                page_number=page["page_number"],
                char_count=len(content),
                content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                content=content,
                created_at=now,
            )
        )


def split_text_into_chunks(content: str, *, target_chars: int) -> list[str]:
    """粘贴文本切段（V25-D-32）：按空行段落贪心打包至 target_chars，段落超限独立成块。

    禁止整块单 chunk：任意非空输入至少产出 1 块，多段落文本必产多块。
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    if not paragraphs:
        paragraphs = [content.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for para in paragraphs:
        para_chars = len(para)
        if para_chars > target_chars:
            if current:
                chunks.append("\n\n".join(current))
                current, current_chars = [], 0
            chunks.append(para)
            continue
        if current and current_chars + para_chars + 2 > target_chars:
            chunks.append("\n\n".join(current))
            current, current_chars = [], 0
        current.append(para)
        current_chars += para_chars + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def persist_text_material_chunks(
    session: Session, *, material_id: str, content: str, target_chars: int, now: str
) -> int:
    """TEXT 资料入库：段落切段为 chunk_seq 1..N（伪页码），返回块数。"""
    pieces = split_text_into_chunks(content, target_chars=target_chars)
    for seq, piece in enumerate(pieces, start=1):
        session.add(
            TextChunk(
                chunk_id=chunk_id_for(material_id, seq, piece),
                file_id=None,
                material_id=material_id,
                chunk_seq=seq,
                page_number=seq,
                char_count=len(piece),
                content_sha256=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                content=piece,
                created_at=now,
            )
        )
    return len(pieces)


def load_pages(
    session: Session,
    *,
    material_id: str,
    start_page: int | None = None,
    end_page: int | None = None,
    file_id: str | None = None,
) -> list[TextChunk]:
    """按页/序升序读取闭区间 [start_page, end_page] 的块文本（material_id 为权威归属）。

    file_id 参数兼容旧调用方（PDF 资料 material_id == file_id，等价换算）；
    TEXT 章节快照页码为 null → 不加页码过滤（整份资料）。
    """
    mid = material_id or file_id
    if mid is None:  # pragma: no cover - 防御
        return []
    query = select(TextChunk).where(TextChunk.material_id == mid)
    if start_page is not None:
        query = query.where(TextChunk.chunk_seq >= start_page)
    if end_page is not None:
        query = query.where(TextChunk.chunk_seq <= end_page)
    return list(session.scalars(query.order_by(TextChunk.chunk_seq)).all())


def page_text_map(chunks: list[TextChunk]) -> dict[int, str]:
    """页文本映射 {page_number: content}（generator 输入形状）。"""
    return {chunk.page_number: chunk.content for chunk in chunks}
