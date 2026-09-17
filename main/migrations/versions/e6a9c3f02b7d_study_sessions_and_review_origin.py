"""学习会话与评分来源（V25-D-37，契约 3.25/3.11/6.6）

- 新表 ``study_sessions``：当日学习会话薄容器，自然键
  ``(user_id, study_date, origin, deck_key)``——同日同来源同范围 begin 命中同一行
  （中断续学仅限当天）；``deck_key`` 为 deck_id 或 '*'，规避 SQLite UNIQUE 对 NULL
  不去重；时长为客户端累计绝对值 max 合并的观测数据。
- ``review_events`` 加 ``origin TEXT NULL``：评分来源 PLAN/BACKLOG/ADHOC，历史行
  保持 NULL=未分类（旧客户端过渡期不失败），不回填不虚构归因。

downgrade 不可实现：评分流水 origin 与会话时长不可无损剥离；按部署纪律以备份恢复替代降级。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a9c3f02b7d"
down_revision: str | Sequence[str] | None = "e5c1d9b7a3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_events", sa.Column("origin", sa.String(), nullable=True))

    op.create_table(
        "study_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("deck_id", sa.String(), nullable=True),
        sa.Column("deck_key", sa.String(), nullable=False),
        sa.Column("study_date", sa.String(), nullable=False),
        sa.Column("study_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("last_reported_at", sa.String(), nullable=True),
        sa.Column("ended_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deck_id"], ["decks.deck_id"], ondelete="CASCADE"),
        sa.CheckConstraint("origin IN ('PLAN','BACKLOG','ADHOC')", name="ck_study_sessions_origin"),
        sa.CheckConstraint(
            "study_seconds >= 0 AND study_seconds <= 86400",
            name="ck_study_sessions_seconds",
        ),
        sa.UniqueConstraint(
            "user_id", "study_date", "origin", "deck_key", name="uq_study_sessions_day_scope"
        ),
    )
    op.create_index(
        "ix_study_sessions_user_date", "study_sessions", ["user_id", "study_date"], unique=False
    )


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：评分流水 origin 与会话时长不可无损剥离；如需回退请从部署备份恢复。"
    )
