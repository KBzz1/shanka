"""account auth pk rebuild fail closed

Revision ID: a7cc699f3fd8
Revises: ddc6f34e30b8
Create Date: 2026-08-14 00:22:53.321943

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7cc699f3fd8"
down_revision: str | Sequence[str] | None = "ddc6f34e30b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER_CHECK = "device_id IS NOT NULL OR user_id IS NOT NULL"

# fail-closed 预检范围：users 计数 + 全部含 user_id 的 owner 表（6 个直接归属表 +
# api_keys / idempotency_keys）的 user_id IS NOT NULL 计数。
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

    一旦存在 user-only 新行，反向 DDL（user_id 列移除、device_id NOT NULL 还原、
    device 主键重建）会丢数据或违反约束；fail closed 拒绝丢弃新数据或合成 device_id。
    空库 / 纯旧 device 域数据副本（users 空、各 owner 表 user_id 全 NULL）允许正常降级。
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
    # review_events：另加 UNIQUE (user_id, client_event_id)；原 UNIQUE (device_id,
    # client_event_id) 保留（batch 重建时反射约束自动携带）。
    with op.batch_alter_table("review_events") as batch_op:
        batch_op.create_unique_constraint(
            "uq_review_events_user_client", ["user_id", "client_event_id"]
        )

    # api_keys 主键重建：device_id PK → user_id PK（user_id TEXT NULL PK + FK → users）。
    # SQLite rowid 表非 INTEGER 主键允许 NULL：user_id 主键 nullable=True 合法，旧行
    # （user_id 为 NULL）可保留、多 NULL 不冲突。batch 模式中 create_primary_key 会移除
    # 反射表上无名的旧主键约束（ApplyBatchImpl.add_constraint），显式命名主键以与 ORM
    # （pk_api_keys）对齐；device_id 遗留 NULL 列与 FK → devices 自动保留（反射约束携带）。
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(), nullable=True))
        batch_op.create_foreign_key("fk_api_keys_user_id", "users", ["user_id"], ["user_id"])
        batch_op.create_primary_key("pk_api_keys", ["user_id"])
        batch_op.alter_column("device_id", nullable=True)
        batch_op.create_check_constraint("ck_api_keys_owner_domain", _OWNER_CHECK)

    # idempotency_keys 主键重建：(device_id, path, idempotency_key) →
    # (user_id, path, idempotency_key)；遗留 UNIQUE (device_id, path, idempotency_key)
    # 保留（旧设备域幂等缓存不跨身份空间重放；SQLite 多 NULL 不冲突）。
    # 跨设备旧行 (path, idempotency_key) 相同也不会冲突：SQLite 对 PK/UNIQUE 中的
    # NULL 视为互异，两行 (NULL, path, idempotency_key) 均保留并存（实测探针证伪
    # "冲突即失败"旧表述）；同设备重放去重由保留的
    # UNIQUE (device_id, path, idempotency_key) 继续兜底（跨设备同键本就允许并存）。
    with op.batch_alter_table("idempotency_keys") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(), nullable=True))
        batch_op.create_primary_key("pk_idempotency_keys", ["user_id", "path", "idempotency_key"])
        batch_op.create_unique_constraint(
            "uq_idempotency_keys_device_path", ["device_id", "path", "idempotency_key"]
        )
        batch_op.alter_column("device_id", nullable=True)
        batch_op.create_check_constraint("ck_idempotency_keys_owner_domain", _OWNER_CHECK)


def downgrade() -> None:
    """Downgrade schema."""
    _fail_closed_check()

    with op.batch_alter_table("review_events") as batch_op:
        batch_op.drop_constraint("uq_review_events_user_client", type_="unique")

    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint("ck_api_keys_owner_domain", type_="check")
        batch_op.drop_constraint("fk_api_keys_user_id", type_="foreignkey")
        batch_op.drop_constraint("pk_api_keys", type_="primary")
        batch_op.drop_column("user_id")
        batch_op.alter_column("device_id", nullable=False)
        batch_op.create_primary_key("pk_api_keys", ["device_id"])

    with op.batch_alter_table("idempotency_keys") as batch_op:
        batch_op.drop_constraint("ck_idempotency_keys_owner_domain", type_="check")
        batch_op.drop_constraint("uq_idempotency_keys_device_path", type_="unique")
        batch_op.drop_constraint("pk_idempotency_keys", type_="primary")
        batch_op.drop_column("user_id")
        batch_op.alter_column("device_id", nullable=False)
        batch_op.create_primary_key("pk_idempotency_keys", ["device_id", "path", "idempotency_key"])
