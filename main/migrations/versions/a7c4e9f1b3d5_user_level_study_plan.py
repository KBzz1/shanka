"""账号级跨项目学习计划（V25-D-39，取代 V25-D-03 单项目规则）

- 新表 ``user_study_settings``（一用户一行，双目标）与 ``user_study_decks``
  （PK (user_id, deck_id)）取代 ``project_study_settings`` /
  ``project_study_decks``：计划从"当前项目的卡组级配置"升级为账号级，
  可选任意本人卡组（跨项目与独立卡组）。
- 存量回填（两层）：优先 ``user_preferences.current_project_id`` 指向项目的
  计划行与目标；该用户没有可回填源时取其 ``updated_at`` 最新的
  project_study_settings 行所属项目。"前计划时代"用户（无任何计划行）回填后
  为诚实空态，重新配置即可。
- ``project_study_settings`` 的 legacy 章节字段（selected_chapter_ids /
  include_unassigned）随表删除：今日计划的章节回退分支同步退役。

downgrade 不可实现：用户级行无法无损还原回单一当前项目的归属关系；按部署
纪律以备份恢复替代降级。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e9f1b3d5"
down_revision: str | Sequence[str] | None = "e6a9c3f02b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_study_settings",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("daily_new_goal", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column("daily_review_goal", sa.Integer(), nullable=False, server_default=sa.text("40")),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "daily_new_goal BETWEEN 0 AND 200 AND daily_new_goal % 10 = 0",
            name="ck_user_study_settings_daily_new_goal",
        ),
        sa.CheckConstraint(
            "daily_review_goal BETWEEN 0 AND 200 AND daily_review_goal % 10 = 0",
            name="ck_user_study_settings_daily_review_goal",
        ),
        sa.CheckConstraint(
            "daily_new_goal + daily_review_goal > 0",
            name="ck_user_study_settings_daily_goal_nonzero",
        ),
    )
    op.create_table(
        "user_study_decks",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("deck_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.deck_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "deck_id", name="pk_user_study_decks"),
    )
    op.create_index("ix_user_study_decks_deck_id", "user_study_decks", ["deck_id"], unique=False)

    # 每用户的回填源项目：优先 current_project_id（该项目须有计划行），否则其
    # updated_at 最新的计划行所属项目；均无则 NULL（诚实空态）。
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _plan_source AS
            SELECT lp.user_id AS user_id, COALESCE(
                (SELECT p.current_project_id FROM user_preferences p
                  WHERE p.user_id = lp.user_id
                    AND p.current_project_id IN
                        (SELECT project_id FROM project_study_settings)),
                (SELECT s2.project_id FROM project_study_settings s2
                  WHERE s2.project_id IN
                        (SELECT project_id FROM learning_projects lp2
                          WHERE lp2.user_id = lp.user_id)
                  ORDER BY s2.updated_at DESC, s2.project_id LIMIT 1)
            ) AS project_id
            FROM learning_projects lp
            GROUP BY lp.user_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO user_study_settings (user_id, daily_new_goal, daily_review_goal, updated_at)
            SELECT src.user_id, s.daily_new_goal, s.daily_review_goal, s.updated_at
            FROM _plan_source src
            JOIN project_study_settings s ON s.project_id = src.project_id
            WHERE src.project_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO user_study_decks (user_id, deck_id, created_at)
            SELECT src.user_id, d.deck_id, d.created_at
            FROM _plan_source src
            JOIN project_study_decks d ON d.project_id = src.project_id
            WHERE src.project_id IS NOT NULL
            """
        )
    )
    op.execute(sa.text("DROP TABLE _plan_source"))

    op.drop_table("project_study_decks")
    op.drop_table("project_study_settings")


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：账号级计划行无法无损还原回单项目归属；如需回退请从部署备份恢复。"
    )
