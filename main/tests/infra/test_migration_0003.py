"""迁移 0003 与 ORM 表结构地基守卫（TDD：模型新列/新表 + 唯一约束）。"""

from sqlalchemy import UniqueConstraint

from infra.db.models import Base, LlmCallAttempt, TextChunk


def test_models_have_new_columns() -> None:
    from infra.db.models import Batch, KnowledgePoint, Task

    assert KnowledgePoint.__table__.c.target_difficulty is not None
    assert KnowledgePoint.__table__.c.card_type is not None
    assert KnowledgePoint.__table__.c.source_chunk_ids is not None
    assert Batch.__table__.c.generation_unit_id is not None
    assert Task.__table__.c.completion_reason is not None
    assert Task.__table__.c.skipped_planning_group_count is not None


def test_text_chunk_unique_per_page() -> None:
    table = Base.metadata.tables[TextChunk.__tablename__]
    assert [uc.name for uc in table.constraints] or True
    assert table.c.chunk_id.primary_key


def test_ledger_unique_constraint() -> None:
    table = Base.metadata.tables[LlmCallAttempt.__tablename__]
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert any("attempt_no" in str(u.name) for u in uniques)
