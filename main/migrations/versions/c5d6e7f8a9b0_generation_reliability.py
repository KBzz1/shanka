"""Generation operation identity, task leases and sample ledger support.

The revision is additive for existing V2.5 databases.  Historical tasks receive an empty lease
state and operation_id=NULL; callers opt into the stronger fencing protocol as they are resumed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "9b3c1a0f4d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "generation_operations",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("operation_key", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ACTIVE"),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("terminal_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.CheckConstraint(
            "status IN ('ACTIVE','COMPLETED','FAILED','ABANDONED')",
            name="ck_generation_operations_status_domain",
        ),
        sa.PrimaryKeyConstraint("operation_id"),
        sa.UniqueConstraint("user_id", "operation_key", name="uq_generation_operations_user_key"),
    )
    op.create_index(
        "ix_generation_operations_user_status",
        "generation_operations",
        ["user_id", "status", "updated_at"],
        unique=False,
    )
    # SQLite partial unique index is the only portable way to allow historical terminal operations
    # while preventing two active operations for the same normalized input.
    op.create_index(
        "ix_generation_operations_active_input",
        "generation_operations",
        ["user_id", "input_fingerprint", "operation_key"],
        unique=True,
        sqlite_where=sa.text("status = 'ACTIVE'"),
    )

    with op.batch_alter_table("tasks", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("operation_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("claimed_by", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("lease_token", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("lease_version", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("lease_until", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(sa.Column("next_attempt_at", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_operation_id",
            "generation_operations",
            ["operation_id"],
            ["operation_id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_tasks_status_domain",
            "status IN ('DRAFT','SAMPLE_GENERATING','AWAITING_SAMPLE_CONFIRMATION',"
            "'GENERATING','COMPLETED','FAILED','ABANDONED')",
        )
        batch_op.create_check_constraint(
            "ck_tasks_stage_domain",
            "stage IS NULL OR stage IN ('PLANNING','GENERATING','SCORING','PUBLISHING')",
        )
        batch_op.create_check_constraint(
            "ck_tasks_lease_fields_together",
            "(claimed_by IS NULL AND lease_token IS NULL AND lease_until IS NULL)"
            " OR (claimed_by IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL)",
        )
        batch_op.create_check_constraint("ck_tasks_lease_version_nonnegative", "lease_version >= 0")
        batch_op.create_check_constraint("ck_tasks_attempt_count_nonnegative", "attempt_count >= 0")

    op.create_index(
        "ix_tasks_queue_claim",
        "tasks",
        ["status", "stage", "next_attempt_at", "lease_until", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_operation_id",
        "tasks",
        ["operation_id"],
        unique=False,
    )

    # Add the sample stage and operation link.  The historical task FK is left intact here; the
    # deletion service explicitly nulls task_id before deleting a task, which is safe on both old
    # and new SQLite files and avoids a destructive table rebuild solely for FK action rewriting.
    with op.batch_alter_table("llm_call_attempts", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("operation_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_llm_call_attempts_operation_id",
            "generation_operations",
            ["operation_id"],
            ["operation_id"],
            ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_llm_call_attempts_stage_domain",
            "stage IN ('SAMPLE','PLANNING','GENERATING','SCORING','REWRITE')",
        )
    op.create_index(
        "ix_llm_call_attempts_operation",
        "llm_call_attempts",
        ["operation_id", "stage", "operation_key"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError("生成可靠性迁移不可逆；回退请恢复升级前备份")
