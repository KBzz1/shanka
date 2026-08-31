"""task queue and deletion preflight indexes.

These are additive indexes only.  They make worker polling and project/deck deletion preflight
bounded by active-resource rows without changing task state semantics.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9b3c1a0f4d2e"
down_revision: str | Sequence[str] | None = "88f2e1abc6f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_tasks_status_stage_updated", "tasks", ["status", "stage", "updated_at"])
    op.create_index(
        "ix_tasks_project_status_updated", "tasks", ["project_id", "status", "updated_at"]
    )
    op.create_index("ix_tasks_deck_status_updated", "tasks", ["deck_id", "status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_deck_status_updated", table_name="tasks")
    op.drop_index("ix_tasks_project_status_updated", table_name="tasks")
    op.drop_index("ix_tasks_status_stage_updated", table_name="tasks")
