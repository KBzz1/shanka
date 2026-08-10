"""add idempotency request_body_hash

Revision ID: ead86a96d103
Revises: 2284b238e3d4
Create Date: 2026-08-10

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ead86a96d103"
down_revision: str | Sequence[str] | None = "2284b238e3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLite 加 NOT NULL 列需要 server_default（已有行时直接 ADD COLUMN 会失败）；
    # 去掉 server_default 用 batch 模式（copy-and-recreate，SQLite 无 DROP DEFAULT）——
    # 幂等表在写入时总是提供该值
    op.add_column(
        "idempotency_keys",
        sa.Column("request_body_hash", sa.String(), nullable=False, server_default=""),
    )
    with op.batch_alter_table("idempotency_keys") as batch_op:
        batch_op.alter_column("request_body_hash", server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("idempotency_keys", "request_body_hash")
