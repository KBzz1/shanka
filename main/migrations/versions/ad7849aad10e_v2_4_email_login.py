"""v2_4_email_login

users 加 email（登录键，NOT NULL + UNIQUE）；username 去唯一（降为展示名）；
清空账号及下游数据（用户裁决：存量测试账号清空重来）。downgrade 显式拒绝（fail-closed）。

Revision ID: ad7849aad10e
Revises: b92357b079ca
"""

import sqlalchemy as sa
from alembic import op

revision = "ad7849aad10e"
down_revision = "b92357b079ca"
branch_labels = None
depends_on = None

# 按 user 隔离的下游表（依赖序无关——env.py 迁移连接层 FK 关闭，P3 已验证；
# 顺序仍按依赖子→父写出以自证）。text_chunks 因迁移连接层 FK 关闭，需显式
# 清空（不依赖 ORM ondelete CASCADE）。
_USER_DOMAIN_TABLES = (
    "chapters",
    "batches",
    "knowledge_points",
    "review_states",
    "review_events",
    "cards",
    "decks",
    "tasks",
    "text_chunks",
    "pdf_files",
    "llm_call_attempts",
    "api_keys",
    "idempotency_keys",
)


def upgrade() -> None:
    # 1) 清空账号及下游数据（V2.4 决策：登录键切换，存量测试账号清空重来）
    for table in _USER_DOMAIN_TABLES:
        op.execute(f"DELETE FROM {table}")
    op.execute("DELETE FROM auth_sessions")
    op.execute("DELETE FROM users")
    # 2) users：username 去唯一 + 加 email（batch 重建；表已空，NOT NULL 直加）
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_username", type_="unique")
        batch_op.add_column(sa.Column("email", sa.String(), nullable=False))
        batch_op.create_unique_constraint("uq_users_email", ["email"])


def downgrade() -> None:
    raise RuntimeError("V2.4 起账号数据已清空且 email 为登录键，迁移不可逆；回退请恢复升级前备份")
