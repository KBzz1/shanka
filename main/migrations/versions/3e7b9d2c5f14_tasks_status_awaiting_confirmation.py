"""tasks 状态域追加 AWAITING_CONFIRMATION（生成结果确认与延迟发布）

V2.5 确认闭环（契约 4.1/6.4；PRD V25-GEN-FR-06 增补）：正式生成完毕不再自动发布，
任务 park 至 ``AWAITING_CONFIRMATION``（卡保持 STAGED），用户 ``POST /tasks/{id}/confirm``
单事务确认发布 → ``COMPLETED``。

- 纯约束域扩展：既有七态全部仍合法，**无数据回填**（存量 COMPLETED 任务本已发布，
  视为已确认）；
- SQLite 需 batch copy-and-recreate 重建 CHECK（0003/ddc6f34e30b8 同款；迁移全程
  env.py 已在连接层关闭外键强制）；
- ORM 侧 ``ck_tasks_status_domain``（Base.metadata.create_all 测试库）保留 legacy
  PENDING/RUNNING/PAUSED 超集，本迁移安装的是生产版八态。

downgrade 不可实现：已 park 的待确认任务无法表达回七态；按部署纪律以备份恢复替代降级。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3e7b9d2c5f14"
down_revision: str | Sequence[str] | None = "b7e4c2a91d50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EIGHT_STATE_CHECK = (
    "status IN ('DRAFT','SAMPLE_GENERATING','AWAITING_SAMPLE_CONFIRMATION',"
    "'GENERATING','AWAITING_CONFIRMATION','COMPLETED','FAILED','ABANDONED')"
)


def upgrade() -> None:
    with op.batch_alter_table("tasks", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_tasks_status_domain", type_="check")
        batch_op.create_check_constraint("ck_tasks_status_domain", _EIGHT_STATE_CHECK)


def downgrade() -> None:
    raise NotImplementedError(
        "不可逆迁移：已 park 的 AWAITING_CONFIRMATION 任务无法表达回七态；"
        "如需回退请从部署备份恢复。"
    )
