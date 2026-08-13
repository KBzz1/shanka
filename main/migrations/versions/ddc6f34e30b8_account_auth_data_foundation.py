"""account auth data foundation

Revision ID: ddc6f34e30b8
Revises: 2a391e994f93
Create Date: 2026-08-13 23:26:23.391536

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ddc6f34e30b8"
down_revision: str | Sequence[str] | None = "2a391e994f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 直接归属 6 表（7.1）：+ user_id（FK → users）、device_id 降级可空、双非空 CHECK、user 查询索引。
# 索引列沿用既有 device 索引的对应后缀（created_at / updated_at / deck_id / reviewed_at）。
_OWNER_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "pdf_files": ("ix_pdf_files_user_created", ("user_id", "created_at")),
    "tasks": ("ix_tasks_user_created", ("user_id", "created_at")),
    "decks": ("ix_decks_user_updated", ("user_id", "updated_at")),
    "cards": ("ix_cards_user_deck", ("user_id", "deck_id")),
    "review_events": ("ix_review_events_user_reviewed", ("user_id", "reviewed_at")),
    "llm_call_attempts": ("ix_llm_call_attempts_user_created", ("user_id", "created_at")),
}

_OWNER_CHECK = "device_id IS NOT NULL OR user_id IS NOT NULL"


def _batch_owner(table: str, *, reverse: bool = False) -> None:
    """直接归属表 batch 重建（SQLite 对既有表加约束需 copy-and-recreate）。

    这些表都是 FK 父表（pdf_files←chapters/text_chunks、tasks←batches/knowledge_points、
    decks←cards、cards←review_states 等）；外键强制开启时 DROP 旧表会触发隐式 DELETE
    并级联误删子表数据，因此迁移全程由 env.py 在连接层关闭外键强制（database-design
    7.1），此处按 0003 同款 batch 模式重建。
    """
    with op.batch_alter_table(table) as batch_op:
        if reverse:
            batch_op.drop_constraint(f"ck_{table}_owner_domain", type_="check")
            batch_op.drop_constraint(f"fk_{table}_user_id", type_="foreignkey")
            batch_op.drop_column("user_id")
            batch_op.alter_column("device_id", nullable=False)
        else:
            batch_op.add_column(sa.Column("user_id", sa.String(), nullable=True))
            batch_op.create_foreign_key(f"fk_{table}_user_id", "users", ["user_id"], ["user_id"])
            batch_op.alter_column("device_id", nullable=True)
            batch_op.create_check_constraint(f"ck_{table}_owner_domain", _OWNER_CHECK)


def upgrade() -> None:
    """Upgrade schema."""
    # 新表 users（7.1：账号数据主体；username 存服务端规范化值，UNIQUE 冲突 → 409 USERNAME_TAKEN）
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    # 新表 auth_sessions（7.1：会话；token 只存 SHA-256 摘要，绝不存明文）
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"], unique=False)

    # 6 个直接归属表：+ user_id、device_id 降级可空、双非空 CHECK（旧行 device_id、新行 user_id）
    for table, (index_name, index_cols) in _OWNER_TABLES.items():
        _batch_owner(table)
        op.create_index(index_name, table, list(index_cols), unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # 反向 DDL（fail-closed 数据预检随主键重建任务一并落地，见 database-design 7.1）
    for table, (index_name, _) in _OWNER_TABLES.items():
        op.drop_index(index_name, table_name=table)
        _batch_owner(table, reverse=True)

    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("users")
