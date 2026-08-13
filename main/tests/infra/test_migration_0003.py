"""迁移 0003 与 ORM 表结构地基守卫（TDD：模型新列/新表 + 唯一约束）。"""

from sqlalchemy import UniqueConstraint

from infra.db.models import Base, Batch, KnowledgePoint, LlmCallAttempt, Task, TextChunk


def test_models_have_new_columns() -> None:
    assert KnowledgePoint.__table__.c.target_difficulty is not None
    assert KnowledgePoint.__table__.c.card_type is not None
    assert KnowledgePoint.__table__.c.source_chunk_ids is not None
    assert Batch.__table__.c.generation_unit_id is not None
    assert Task.__table__.c.completion_reason is not None
    assert Task.__table__.c.skipped_planning_group_count is not None


def test_text_chunk_unique_per_page() -> None:
    table = Base.metadata.tables[TextChunk.__tablename__]
    assert table.c.chunk_id.primary_key
    assert any(
        isinstance(c, UniqueConstraint)
        and {col.name for col in c.columns} == {"file_id", "page_number"}
        for c in table.constraints
    )


def test_ledger_unique_constraint() -> None:
    table = Base.metadata.tables[LlmCallAttempt.__tablename__]
    uniques = [c for c in table.constraints if isinstance(c, UniqueConstraint)]
    assert any(
        {col.name for col in u.columns}
        == {"scope_type", "scope_id", "stage", "operation_key", "attempt_no"}
        for u in uniques
    )


def test_batches_generation_unit_unique() -> None:
    table = Base.metadata.tables[Batch.__tablename__]
    assert any(
        {col.name for col in c.columns} == {"task_id", "generation_unit_id"}
        for c in table.constraints
        if isinstance(c, UniqueConstraint)
    )
