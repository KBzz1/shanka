"""llm_pipeline_upgrade

Revision ID: 2a391e994f93
Revises: ead86a96d103
Create Date: 2026-08-13 11:53:36.705008

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2a391e994f93"
down_revision: str | Sequence[str] | None = "ead86a96d103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 新表 text_chunks（spec §4.1/§11：页文本一页一行、与章节解耦；file_id 删除级联清理）
    op.create_table(
        "text_chunks",
        sa.Column("chunk_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["pdf_files.file_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("file_id", "page_number", name="uq_text_chunks_file_page"),
    )
    op.create_index(
        "ix_text_chunks_file_page", "text_chunks", ["file_id", "page_number"], unique=False
    )

    # 新表 llm_call_attempts（spec §9：调用账本；task_id 可空，级联删除；attempt 唯一约束）
    op.create_table(
        "llm_call_attempts",
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("operation_key", sa.String(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_name", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("schema_name", sa.String(), nullable=True),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("rubric_version", sa.String(), nullable=True),
        sa.Column("cache_hit", sa.Integer(), nullable=True),
        sa.Column("cache_miss", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("normalized_result", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "stage",
            "operation_key",
            "attempt_no",
            name="uq_llm_call_attempts_scope_attempt_no",
        ),
    )
    op.create_index(
        "ix_llm_call_attempts_device_created",
        "llm_call_attempts",
        ["device_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_llm_call_attempts_task_stage_operation",
        "llm_call_attempts",
        ["task_id", "stage", "operation_key"],
        unique=False,
    )

    # batches：+ generation_unit_id（FK knowledge_points ON DELETE SET NULL）+ UNIQUE(task_id, generation_unit_id)
    # SQLite 对既有表加约束需 batch copy-and-recreate
    with op.batch_alter_table("batches") as batch_op:
        batch_op.add_column(sa.Column("generation_unit_id", sa.String(), nullable=True))
        batch_op.create_unique_constraint("uq_batches_task_unit", ["task_id", "generation_unit_id"])
        batch_op.create_foreign_key(
            "fk_batches_generation_unit_id",
            "knowledge_points",
            ["generation_unit_id"],
            ["knowledge_point_id"],
            ondelete="SET NULL",
        )

    # knowledge_points 新列（spec §11）
    op.add_column("knowledge_points", sa.Column("target_difficulty", sa.String(), nullable=True))
    op.add_column("knowledge_points", sa.Column("card_type", sa.String(), nullable=True))
    op.add_column("knowledge_points", sa.Column("source_chunk_ids", sa.Text(), nullable=True))

    # tasks 新列（spec §11）：skipped_planning_group_count NOT NULL 需 server_default（既有行 + 裸 SQL 插入路径）
    op.add_column("tasks", sa.Column("completion_reason", sa.String(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "skipped_planning_group_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("batches") as batch_op:
        batch_op.drop_constraint("fk_batches_generation_unit_id", type_="foreignkey")
        batch_op.drop_constraint("uq_batches_task_unit", type_="unique")
        batch_op.drop_column("generation_unit_id")

    op.drop_column("tasks", "skipped_planning_group_count")
    op.drop_column("tasks", "completion_reason")

    op.drop_column("knowledge_points", "source_chunk_ids")
    op.drop_column("knowledge_points", "card_type")
    op.drop_column("knowledge_points", "target_difficulty")

    op.drop_index("ix_llm_call_attempts_task_stage_operation", table_name="llm_call_attempts")
    op.drop_index("ix_llm_call_attempts_device_created", table_name="llm_call_attempts")
    op.drop_table("llm_call_attempts")
    op.drop_index("ix_text_chunks_file_page", table_name="text_chunks")
    op.drop_table("text_chunks")
