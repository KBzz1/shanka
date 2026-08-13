"""planning.py：KnowledgePoint 规划（5.4.1/3.6；COMPACT≤BALANCED≤EXTENSIVE 可测口径）。

规划为纯计算：返回未持久化对象（不 add 到 session）——knowledge_points.task_id
FK → tasks（engine 级 PRAGMA foreign_keys=ON），落库由调用方在 Task 行存在后
同事务 session.add_all（见 services.tasks.service.create_task）。
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.db.models import Chapter, KnowledgePoint
from services.generation.quota import task_unit_budget


def plan_knowledge_points(
    session: Session, *, task_id: str, chapter_ids: list[str], quantity_tendency: str
) -> list[KnowledgePoint]:
    """每章确定性分块：chunk_count = 基础 3 × 密度系数（COMPACT=1/BALANCED=2/EXTENSIVE=3）。

    topic 用"第X章-知识点N"确定性命名；source_chunk_id 含 chapter_id。
    R-13 注释：真实分块（章节文本抽取）在 V5A 或后续接入，V4 规划结构正确。
    """
    chunks_per_chapter = task_unit_budget(1, quantity_tendency)
    chapters = session.scalars(select(Chapter).where(Chapter.chapter_id.in_(chapter_ids))).all()
    kps: list[KnowledgePoint] = []
    for ch in chapters:
        for i in range(chunks_per_chapter):
            kps.append(
                KnowledgePoint(
                    knowledge_point_id=str(uuid.uuid4()),
                    task_id=task_id,
                    chapter_id=ch.chapter_id,
                    source_chunk_id=f"{ch.chapter_id}:chunk{i + 1}",
                    topic=f"{ch.name}-知识点{i + 1}",
                    priority=i + 1,
                    status="PENDING",
                )
            )
    return kps
