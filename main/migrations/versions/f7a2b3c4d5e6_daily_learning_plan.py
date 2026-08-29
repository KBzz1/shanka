"""Add deck-scoped daily learning plans and parser fencing fields.

The legacy chapter selection columns remain readable for old generation flows.  The new daily
learning planner stores its independently editable deck set in ``project_study_decks`` and its
two quotas on ``project_study_settings``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pdf_files", sa.Column("parse_lease_token", sa.String(), nullable=True))
    op.add_column("pdf_files", sa.Column("parse_lease_until", sa.String(), nullable=True))
    op.add_column(
        "pdf_files",
        sa.Column("parse_version", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    with op.batch_alter_table("project_study_settings", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("daily_new_goal", sa.Integer(), nullable=False, server_default=sa.text("10"))
        )
        batch_op.add_column(
            sa.Column(
                "daily_review_goal", sa.Integer(), nullable=False, server_default=sa.text("40")
            )
        )
        batch_op.create_check_constraint(
            "ck_project_study_settings_daily_new_goal",
            "daily_new_goal BETWEEN 0 AND 200 AND daily_new_goal % 10 = 0",
        )
        batch_op.create_check_constraint(
            "ck_project_study_settings_daily_review_goal",
            "daily_review_goal BETWEEN 0 AND 200 AND daily_review_goal % 10 = 0",
        )
        batch_op.create_check_constraint(
            "ck_project_study_settings_daily_goal_nonzero",
            "daily_new_goal + daily_review_goal > 0",
        )

    # Existing project shells have no deck-scoped plan row.  Materialize an explicit empty plan
    # for them so the new product never silently queues every project deck after upgrade.
    op.execute(
        sa.text(
            "INSERT INTO project_study_settings "
            "(project_id, selected_chapter_ids, include_unassigned, daily_new_goal, "
            "daily_review_goal, updated_at) "
            "SELECT p.project_id, '[]', 0, 10, 40, p.updated_at "
            "FROM learning_projects AS p "
            "WHERE NOT EXISTS (SELECT 1 FROM project_study_settings AS s "
            "WHERE s.project_id = p.project_id)"
        )
    )

    op.create_table(
        "project_study_decks",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("deck_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["learning_projects.project_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.deck_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "deck_id", name="pk_project_study_decks"),
    )
    op.create_index(
        "ix_project_study_decks_deck_id", "project_study_decks", ["deck_id"], unique=False
    )


def downgrade() -> None:
    raise RuntimeError("每日学习计划与解析租约迁移不可逆；回退请恢复升级前备份")
