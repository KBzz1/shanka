"""V2.3 设备架构彻底清除（不可逆）

Revision ID: b92357b079ca
Revises: e85c78b2a345
"""

from alembic import op

revision = "b92357b079ca"
down_revision = "e85c78b2a345"
branch_labels = None
depends_on = None

# 子表先行（FK 已由 env.py 关闭，此处按依赖序保证语义清晰）
_DELETE_ORDER = (
    "review_events",  # → cards
    "cards",  # → decks
    "decks",
    "llm_call_attempts",  # → tasks
    "tasks",  # → pdf_files
    "api_keys",
    "idempotency_keys",
    "pdf_files",
)
_CHECK_CONSTRAINTS = {
    "api_keys": "ck_api_keys_owner_domain",
    "pdf_files": "ck_pdf_files_owner_domain",
    "tasks": "ck_tasks_owner_domain",
    "decks": "ck_decks_owner_domain",
    "cards": "ck_cards_owner_domain",
    "review_events": "ck_review_events_owner_domain",
    "idempotency_keys": "ck_idempotency_keys_owner_domain",
    "llm_call_attempts": "ck_llm_call_attempts_owner_domain",
}
_DEVICE_UNIQUES = (
    ("idempotency_keys", "uq_idempotency_keys_device_path"),
    ("api_keys", "uq_api_keys_device_id"),
    ("review_events", "uq_review_events_device_client"),
)
_DEVICE_INDEXES = {
    "pdf_files": ["ix_pdf_files_device_created"],
    "tasks": ["ix_tasks_device_created", "ix_tasks_task_device"],
    "decks": ["ix_decks_device_updated"],
    "cards": ["ix_cards_device_deck"],
    "review_events": ["ix_review_events_device_reviewed"],
    "llm_call_attempts": ["ix_llm_call_attempts_device_created"],
}


def upgrade() -> None:
    # 1. 旧 device 域行物理删除（user_id IS NULL 即旧 device 域行）
    for table in _DELETE_ORDER:
        op.execute(f"DELETE FROM {table} WHERE user_id IS NULL")
    # 2. 8 表删除 CHECK 双非空约束（先于删列：CHECK 引用 device_id）
    for table, ck in _CHECK_CONSTRAINTS.items():
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(ck, type_="check")
    # 3. 删除 3 个 device 版 UNIQUE（先于删列：约束引用 device_id）
    for table, uq in _DEVICE_UNIQUES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(uq, type_="unique")
    # 4. 删除 6 个 device_ 索引（先于删列：索引引用 device_id，SQLite 重建失败）
    for table, indexes in _DEVICE_INDEXES.items():
        with op.batch_alter_table(table) as batch:
            for idx in indexes:
                batch.drop_index(idx)
    # 5. 8 表删除 device_id 列
    for table in _DELETE_ORDER:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("device_id")
    # 6. 删除 devices 表
    op.drop_table("devices")


def downgrade() -> None:
    raise RuntimeError("V2.3 起设备数据已物理删除，迁移不可逆；回退请恢复升级前备份")
