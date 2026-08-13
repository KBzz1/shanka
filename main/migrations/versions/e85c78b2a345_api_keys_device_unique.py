"""api keys device unique

Revision ID: e85c78b2a345
Revises: a7cc699f3fd8
Create Date: 2026-08-14 05:14:52.089036

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e85c78b2a345"
down_revision: str | Sequence[str] | None = "a7cc699f3fd8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# fail-closed 预检范围（与 a7cc699f3fd8 同款）：users 计数 + 全部含 user_id 的 owner 表
# 的 user_id IS NOT NULL 计数。
_OWNER_TABLES: tuple[str, ...] = (
    "pdf_files",
    "tasks",
    "decks",
    "cards",
    "review_events",
    "llm_call_attempts",
    "api_keys",
    "idempotency_keys",
)


def _fail_closed_check() -> None:
    """downgrade 前置检查（在任何 DDL/DML 之前）：存在用户域数据 → 拒绝降级。

    本 revision 的 downgrade 先于 a7cc699f3fd8 的 fail-closed 预检执行——若不先自检，
    撤 UNIQUE(device_id) 的 DDL 会在 a7cc 拒绝前落地，破坏「拒绝后表结构不变」不变式。
    """
    bind = op.get_bind()
    users_count = bind.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one() or 0
    owner_counts = {
        table: bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NOT NULL")
        ).scalar_one()
        or 0
        for table in _OWNER_TABLES
    }
    if users_count > 0 or any(owner_counts.values()):
        raise RuntimeError("user-domain data exists; downgrade refused (fail closed)")


def upgrade() -> None:
    """Upgrade schema."""
    # 补回 v2.1 每设备唯一性保障（api_keys PK 由 device_id 重建为 user_id 时丢失，
    # P4 跟进 a）：UNIQUE (device_id)。SQLite UNIQUE 对 NULL 视为互异——用户域行
    # device_id NULL 任意多行不冲突；遗留设备域同 device_id 防重。SQLite 加约束需
    # batch copy-and-recreate（既有 PK/FK/CHECK 由反射携带）。
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.create_unique_constraint("uq_api_keys_device_id", ["device_id"])


def downgrade() -> None:
    """Downgrade schema."""
    _fail_closed_check()
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint("uq_api_keys_device_id", type_="unique")
