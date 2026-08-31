"""knowledge_points 增加 coverage_tier 列（V25-D-25 密度制：Planner 覆盖层级落库并传给生成）。

Revision ID: a3f8d21c9e47
Revises: f7a2b3c4d5e6
Create Date: 2026-08-31

可逆：该列可空、无默认值语义迁移，down 直接删列。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f8d21c9e47"
down_revision: str | Sequence[str] | None = "f7a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_points",
        sa.Column("coverage_tier", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_points", "coverage_tier")
