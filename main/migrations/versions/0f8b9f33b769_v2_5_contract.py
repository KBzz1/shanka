"""v2_5_contract

V2.5 契约转正（database-design 7.3：学习项目与整批发布；不可逆）。

- 新表：learning_projects / user_preferences / project_study_settings /
  card_deletion_batches / card_rewrite_previews（2.17~2.21）。
- 现有表调整（2.5/2.8/2.9/2.15/2.16）：
  - users 加 avatar_key NOT NULL DEFAULT 'mood_01'；
  - decks 加 project_id NULL FK → learning_projects SET NULL + (project_id) 索引；
  - tasks 加 project_id/retry_of_task_id/sample_cards/sample_config_hash/
    sample_confirmed_at + (project_id) 索引；状态迁移 PENDING→DRAFT、
    RUNNING→GENERATING、COMPLETED→COMPLETED、FAILED→FAILED、
    CANCELLED→ABANDONED、PAUSED→FAILED(+LEGACY_PAUSED_TASK)；
  - cards 加 source_task_id/chapter_id/publication_state(默认 PUBLISHED)/
    delete_batch_id/pending_delete_at/undo_until + 3 索引；历史卡均迁 PUBLISHED；
  - knowledge_points/cards 历史 target_difficulty='APPLICATION' → 'DEEP_QUESTION'；
  - review_events.device_timezone 改为可空审计字段（不删除历史值）。
- 回填（7.3）：每个现有 PDF 建一个学习项目（名称取 filename 去扩展名；
  PARSED 项目 chapters_confirmed_at=migrated_at，其余按 PDF 状态映射 NULL）；
  既有 GENERATED 牌组若能从 task.file_id 唯一定位项目则绑定，否则保持独立；
  现有 task 绑定其 file 对应项目；file_id=null 的既有终态任务保留为只读历史。
  无法归属的牌组/任务数量经 INFO 日志记录（迁移报告）。
- 删除不可逆：downgrade 第一行 raise（fail-closed，延续 V2.3/V2.4 精神）；
  回退仅限恢复升级前备份。

Revision ID: 0f8b9f33b769
Revises: ad7849aad10e（运行时 alembic heads 的 V2.4 终态 head）
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from domain.task import (
    DIFFICULTY_V25_MIGRATION,
    LEGACY_PAUSED_TASK_ERROR_CODE,
    TASK_STATUS_V25_MIGRATION,
)

revision: str = "0f8b9f33b769"
down_revision: str | Sequence[str] | None = "ad7849aad10e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _migrated_at() -> str:
    """回填时间戳（database-design 0 统一格式：UTC、恒 3 位毫秒，与 format_utc 同口径）。"""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def upgrade() -> None:
    migrated_at = _migrated_at()

    # ---------- 1) 新表（database-design 2.17~2.21） ----------
    op.create_table(
        "learning_projects",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("file_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("chapters_confirmed_at", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["file_id"], ["pdf_files.file_id"]),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("file_id", name="uq_learning_projects_file_id"),
    )
    op.create_index(
        "ix_learning_projects_user_updated",
        "learning_projects",
        ["user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("coverage_mode", sa.String(), nullable=False, server_default="BALANCED"),
        sa.Column("basic_ratio", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("understanding_ratio", sa.Integer(), nullable=False, server_default="40"),
        sa.Column("deep_question_ratio", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("daily_goal", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("learning_timezone", sa.String(), nullable=False),
        sa.Column("current_project_id", sa.String(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(
            ["current_project_id"], ["learning_projects.project_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint(
            "basic_ratio % 10 = 0 AND basic_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_basic_ratio",
        ),
        sa.CheckConstraint(
            "understanding_ratio % 10 = 0 AND understanding_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_understanding_ratio",
        ),
        sa.CheckConstraint(
            "deep_question_ratio % 10 = 0 AND deep_question_ratio BETWEEN 0 AND 100",
            name="ck_user_prefs_deep_question_ratio",
        ),
        sa.CheckConstraint(
            "basic_ratio + understanding_ratio + deep_question_ratio = 100",
            name="ck_user_prefs_ratio_total",
        ),
        sa.CheckConstraint(
            "daily_goal BETWEEN 10 AND 200 AND daily_goal % 10 = 0",
            name="ck_user_prefs_daily_goal",
        ),
    )

    op.create_table(
        "project_study_settings",
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("selected_chapter_ids", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("include_unassigned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"], ["learning_projects.project_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("project_id"),
    )

    op.create_table(
        "card_deletion_batches",
        sa.Column("delete_batch_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("undo_until", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("delete_batch_id"),
    )
    op.create_index(
        "ix_deletion_batches_user_status_undo",
        "card_deletion_batches",
        ["user_id", "status", "undo_until"],
        unique=False,
    )

    op.create_table(
        "card_rewrite_previews",
        sa.Column("rewrite_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("card_id", sa.String(), nullable=False),
        sa.Column("base_card_version", sa.String(), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("custom_requirements", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["card_id"], ["cards.card_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rewrite_id"),
    )
    op.create_index(
        "ix_rewrite_previews_user_status_expires",
        "card_rewrite_previews",
        ["user_id", "status", "expires_at"],
        unique=False,
    )

    # ---------- 2) 现有表调整（database-design 2.5/2.8/2.9/2.15/2.11） ----------
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column("avatar_key", sa.String(), nullable=False, server_default="mood_01")
        )

    with op.batch_alter_table("decks") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_decks_project_id",
            "learning_projects",
            ["project_id"],
            ["project_id"],
            ondelete="SET NULL",
        )
    op.create_index("ix_decks_project_id", "decks", ["project_id"], unique=False)

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_project_id",
            "learning_projects",
            ["project_id"],
            ["project_id"],
            ondelete="SET NULL",
        )
        batch_op.add_column(sa.Column("retry_of_task_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_retry_of_task",
            "tasks",
            ["retry_of_task_id"],
            ["task_id"],
            ondelete="SET NULL",
        )
        batch_op.add_column(sa.Column("sample_cards", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("sample_config_hash", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sample_confirmed_at", sa.String(), nullable=True))
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"], unique=False)

    with op.batch_alter_table("cards") as batch_op:
        batch_op.add_column(sa.Column("source_task_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_cards_source_task", "tasks", ["source_task_id"], ["task_id"], ondelete="SET NULL"
        )
        batch_op.add_column(sa.Column("chapter_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_cards_chapter_id", "chapters", ["chapter_id"], ["chapter_id"], ondelete="SET NULL"
        )
        batch_op.add_column(
            sa.Column(
                "publication_state",
                sa.String(),
                nullable=False,
                server_default="PUBLISHED",
            )
        )
        batch_op.add_column(sa.Column("delete_batch_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_cards_delete_batch",
            "card_deletion_batches",
            ["delete_batch_id"],
            ["delete_batch_id"],
            ondelete="SET NULL",
        )
        batch_op.add_column(sa.Column("pending_delete_at", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("undo_until", sa.String(), nullable=True))
    op.create_index("ix_cards_source_task", "cards", ["source_task_id"], unique=False)
    op.create_index("ix_cards_chapter_id", "cards", ["chapter_id"], unique=False)
    op.create_index(
        "ix_cards_publication_delete",
        "cards",
        ["publication_state", "delete_batch_id"],
        unique=False,
    )

    with op.batch_alter_table("review_events") as batch_op:
        batch_op.alter_column(
            "device_timezone", existing_type=sa.String(), nullable=True
        )  # V2.5 可空审计字段，不删除历史值

    # ---------- 3) 数据迁移（database-design 7.3） ----------
    # 任务状态迁移：PENDING→DRAFT、RUNNING→GENERATING、COMPLETED/FAILED 原样、
    # CANCELLED→ABANDONED；PAUSED→FAILED 并同一条 UPDATE 写 LEGACY_PAUSED_TASK
    # 占位（禁止留下 V2.5 不可表达状态；映射与占位码原子同写，避免误标原 FAILED 行）
    for legacy_status, v25_status in TASK_STATUS_V25_MIGRATION.items():
        if legacy_status == "PAUSED":
            op.execute(
                "UPDATE tasks SET status = 'FAILED', error_code = '"
                + LEGACY_PAUSED_TASK_ERROR_CODE
                + "' WHERE status = 'PAUSED'"
            )
            continue
        op.execute(f"UPDATE tasks SET status = '{v25_status}' WHERE status = '{legacy_status}'")

    # APPLICATION → DEEP_QUESTION（knowledge_points + cards，契约 3.5/3.6）
    for table in ("knowledge_points", "cards"):
        for legacy, v25 in DIFFICULTY_V25_MIGRATION.items():
            op.execute(
                f"UPDATE {table} SET target_difficulty = '{v25}'"
                f" WHERE target_difficulty = '{legacy}'"
            )

    # 回填：每个现有 PDF 建一个学习项目（名称取 filename 去扩展名；
    # PARSED 项目 chapters_confirmed_at = migrated_at，其他按 PDF 状态映射 NULL）
    bind = op.get_bind()
    pdf_rows = bind.execute(
        sa.text("SELECT file_id, user_id, filename, status FROM pdf_files")
    ).fetchall()
    for file_id, user_id, filename, status in pdf_rows:
        name = filename.rsplit(".", 1)[0] if "." in filename else filename
        confirmed_at = migrated_at if status == "PARSED" else None
        bind.execute(
            sa.text(
                "INSERT INTO learning_projects (project_id, user_id, file_id, name,"
                " chapters_confirmed_at, version, created_at, updated_at)"
                " VALUES (:project_id, :user_id, :file_id, :name, :confirmed_at,"
                " :version, :created_at, :updated_at)"
            ),
            {
                "project_id": str(uuid.uuid4()),
                "user_id": user_id,
                "file_id": file_id,
                "name": name,
                "confirmed_at": confirmed_at,
                "version": migrated_at,  # 缓存版本：回填即当前版本
                "created_at": migrated_at,
                "updated_at": migrated_at,
            },
        )

    # 任务绑定：task 绑定其 file 对应项目；file_id=null 的终态历史任务保持 NULL（只读历史）
    op.execute(
        "UPDATE tasks SET project_id = ("
        "  SELECT project_id FROM learning_projects lp WHERE lp.file_id = tasks.file_id"
        ") WHERE file_id IS NOT NULL"
    )

    # 既有 GENERATED 牌组：若能从 task.file_id 唯一定位项目则绑定，否则保持独立
    unbound_decks = 0
    deck_rows = bind.execute(
        sa.text(
            "SELECT d.deck_id, d.user_id, COUNT(DISTINCT t.file_id) AS file_count,"
            " MAX(t.file_id) AS file_id"
            " FROM decks d LEFT JOIN tasks t ON t.deck_id = d.deck_id"
            " WHERE d.source = 'GENERATED'"
            " GROUP BY d.deck_id, d.user_id"
        )
    ).fetchall()
    for deck_id, _user_id, file_count, file_id in deck_rows:
        if file_count == 1 and file_id is not None:
            project_id = bind.execute(
                sa.text("SELECT project_id FROM learning_projects WHERE file_id = :fid"),
                {"fid": file_id},
            ).scalar()
            if project_id is not None:
                bind.execute(
                    sa.text("UPDATE decks SET project_id = :pid WHERE deck_id = :did"),
                    {"pid": project_id, "did": deck_id},
                )
                continue
        unbound_decks += 1
    if unbound_decks:
        logger.warning("V2.5 迁移：%d 个 GENERATED 牌组无法唯一定位项目，保持独立", unbound_decks)
    else:
        logger.info("V2.5 迁移：全部 GENERATED 牌组均唯一定位并绑定项目")

    orphan_tasks = bind.execute(
        sa.text("SELECT COUNT(*) FROM tasks WHERE file_id IS NOT NULL AND project_id IS NULL")
    ).scalar()
    if orphan_tasks:
        logger.warning("V2.5 迁移：%d 个任务未能绑定项目（file 无对应项目）", orphan_tasks)
    else:
        logger.info("V2.5 迁移：全部有 file 的任务均绑定项目")


def downgrade() -> None:
    raise RuntimeError("V2.5 学习项目与整批发布迁移不可逆；回退请恢复升级前备份")
