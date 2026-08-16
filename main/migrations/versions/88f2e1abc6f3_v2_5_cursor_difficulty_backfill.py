"""v2_5_cursor_difficulty_backfill

V2.5 契约缺口回填（0f8b9f33b769 遗漏项；不可逆，downgrade fail-closed）。

0f8b9f33b769 把 cards/knowledge_points 的历史 target_difficulty='APPLICATION'
→ 'DEEP_QUESTION'，但漏了 tasks.cursor 快照里的难度键——历史任务的
cursor.difficulty_distribution 仍含旧键 APPLICATION，GET /tasks 透传原始
JSON，客户端按 V2.5 难度枚举（BASIC/UNDERSTANDING/DEEP_QUESTION）解析失败
（视觉 lane 报"任务列表不能完整解析"）。

本迁移逐行转换：difficulty_distribution 的 APPLICATION 键并入
DEEP_QUESTION（同名已存在时数值相加），删去 APPLICATION；无旧键的行不动
（幂等）。

Revision ID: 88f2e1abc6f3
Revises: 30364748ec32
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "88f2e1abc6f3"
down_revision: str | Sequence[str] | None = "30364748ec32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _migrate_cursor(cursor: dict[str, Any]) -> dict[str, Any] | None:
    """APPLICATION 键并入 DEEP_QUESTION；无旧键返回 None（跳过写库）。"""
    distribution = cursor.get("difficulty_distribution")
    if not isinstance(distribution, dict) or "APPLICATION" not in distribution:
        return None
    out = dict(cursor)
    migrated = dict(distribution)
    legacy_count = migrated.pop("APPLICATION")
    migrated["DEEP_QUESTION"] = migrated.get("DEEP_QUESTION", 0) + legacy_count
    out["difficulty_distribution"] = migrated
    return out


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT task_id, cursor FROM tasks WHERE cursor IS NOT NULL")
    ).fetchall()
    updated = 0
    for task_id, raw in rows:
        migrated = _migrate_cursor(json.loads(raw))
        if migrated is None:
            continue
        bind.execute(
            sa.text("UPDATE tasks SET cursor = :cursor WHERE task_id = :task_id"),
            {"cursor": json.dumps(migrated, ensure_ascii=False), "task_id": task_id},
        )
        updated += 1
    logger.info("V2.5 cursor 难度键回填：%d/%d 行已转换", updated, len(rows))


def downgrade() -> None:
    raise RuntimeError("迁移不可逆：cursor 已转为 V2.5 难度键，回退仅限恢复升级前备份")
