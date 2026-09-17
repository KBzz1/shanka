"""V25-D-38：chapters.source CHECK 域追加 HEADING（HTML 标题章节）与 AUTO（程序阈值单章）

纯约束域扩展：既有值全部仍合法，无数据回填。SQLite 需 batch copy-and-recreate 重建
CHECK（0003/3e7b9d2c5f14 同款；迁移全程 env.py 已在连接层关闭外键强制）。

downgrade 不可实现：已存在的 HEADING/AUTO 行无法表达回旧域；按部署纪律以备份恢复替代降级。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5c1d9b7a3f2"
down_revision: str | Sequence[str] | None = "d4f8a2c6b1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_DOMAIN = "source IN ('TOC','HEADING','AI','AUTO','FALLBACK','TEXT','ZIP','MANUAL')"


def upgrade() -> None:
    with op.batch_alter_table("chapters", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_chapters_source_domain", type_="check")
        batch_op.create_check_constraint("ck_chapters_source_domain", _SOURCE_DOMAIN)


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：HEADING/AUTO 章节来源无法表达回旧域；如需回退请从部署备份恢复。"
    )
