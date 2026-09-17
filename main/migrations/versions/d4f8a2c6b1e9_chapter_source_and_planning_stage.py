"""V25-D-36 章节来源与章节规划账本域

- ``chapters.source``：章节初始来源（TOC/AI/FALLBACK/TEXT/ZIP/MANUAL，用户改章不改 source）；
  存量行按资料类型回填（PDF→TOC、TEXT→TEXT、ZIP→ZIP）；
- ``llm_call_attempts.stage`` CHECK 域追加 ``CHAPTER_PLANNING``（无目录 PDF 的 AI 章节
  边界规划调用，scope_type=MATERIAL）。

SQLite 需 batch copy-and-recreate 重建 CHECK（0003/3e7b9d2c5f14 同款；迁移全程 env.py
已在连接层关闭外键强制）。

downgrade 不可实现：llm_call_attempts 已存在的 CHAPTER_PLANNING 行无法表达回旧域，
chapters.source 语义删除后 AI/FALLBACK 章节来源不可追溯；按部署纪律以备份恢复替代降级。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c6b1e9"
down_revision: str | Sequence[str] | None = "3e7b9d2c5f14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STAGE_DOMAIN = "stage IN ('SAMPLE','PLANNING','GENERATING','SCORING','REWRITE','CHAPTER_PLANNING')"

_SOURCE_DOMAIN = "source IN ('TOC','AI','FALLBACK','TEXT','ZIP','MANUAL')"


def upgrade() -> None:
    # 1) chapters.source：先以 server_default 加列（存量 PDF 章节落 TOC），再按资料类型回填
    op.add_column(
        "chapters",
        sa.Column("source", sa.String(), nullable=False, server_default="TOC"),
    )
    op.execute(
        "UPDATE chapters SET source = 'TEXT' "
        "WHERE material_id IN (SELECT material_id FROM materials WHERE type = 'TEXT')"
    )
    op.execute(
        "UPDATE chapters SET source = 'ZIP' "
        "WHERE material_id IN (SELECT material_id FROM materials WHERE type = 'ZIP')"
    )
    with op.batch_alter_table("chapters", recreate="always") as batch_op:
        batch_op.alter_column("source", existing_type=sa.String(), server_default=None)
        batch_op.create_check_constraint("ck_chapters_source_domain", _SOURCE_DOMAIN)

    # 2) llm_call_attempts.stage 域扩展
    with op.batch_alter_table("llm_call_attempts", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_llm_call_attempts_stage_domain", type_="check")
        batch_op.create_check_constraint("ck_llm_call_attempts_stage_domain", _STAGE_DOMAIN)


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：CHAPTER_PLANNING 账本行与 AI/FALLBACK 章节来源无法表达回旧域；"
        "如需回退请从部署备份恢复。"
    )
