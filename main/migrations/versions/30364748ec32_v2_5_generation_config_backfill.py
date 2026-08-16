"""v2_5_generation_config_backfill

V2.5 契约缺口回填（0f8b9f33b769 遗漏项；不可逆，downgrade fail-closed）。

0f8b9f33b769 完成了任务状态改名与项目回填，但未迁移 tasks.generation_config
的 V2.4 → V2.5 字段形态。历史任务行因此缺 coverage_mode、难度比例仍是
application 浮点档——GET /tasks 透传原始 JSON，客户端按 OpenAPI
GenerationConfig 解析失败（视觉 lane 真机验证发现）。

本迁移逐行转换（已 V2.5 形态的行不动，幂等）：
- quantity_tendency → coverage_mode（COMPACT/BALANCED/EXTENSIVE 同名映射；
  缺失/非法值 → BALANCED，domain.preferences.DEFAULT_COVERAGE_MODE）；
- difficulty_ratio：application → deep_question；浮点 0~1 → 0~100 的
  10% 整数档（合计恒 100，残差并入占比最大档；全 0 → 100/0/0）；
- 缺失项补 V2.5 默认（BALANCED + 40/40/20 + custom_requirements null）。

Revision ID: 30364748ec32
Revises: 0f8b9f33b769
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from domain.preferences import DEFAULT_COVERAGE_MODE, DEFAULT_DIFFICULTY_RATIO

revision: str = "30364748ec32"
down_revision: str | Sequence[str] | None = "0f8b9f33b769"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_COVERAGE_MODES = {"COMPACT", "BALANCED", "EXTENSIVE"}


def _pct_step(value: float) -> int:
    """浮点 0~1 比例（V2.4）或整数百分比 → 0~100 的 10% 整数档。"""
    pct = value * 100 if 0 <= value <= 1 else value
    return int(round(pct / 10) * 10)


def _ratio_from_legacy(ratio: dict[str, Any]) -> dict[str, int]:
    """V2.4 难度比例（application 键、浮点 0~1）→ V2.5 三档（10% 整数、合计 100）。"""
    legacy = {
        "basic": ratio.get("basic", 0.0),
        "understanding": ratio.get("understanding", 0.0),
        "deep_question": ratio.get("application", ratio.get("deep_question", 0.0)),
    }
    rounded = {key: _pct_step(float(value)) for key, value in legacy.items()}
    # 每档均为 10 的倍数 → 残差同为 10 的倍数，并入占比最大档后合计恒 100
    # （round 误差如 0.33/0.33/0.34 → 30/30/30，diff=10 入 basic）
    diff = 100 - sum(rounded.values())
    if diff:
        largest = max(rounded, key=lambda k: rounded[k])
        rounded[largest] += diff
    return rounded


def _migrate_generation_config(cfg: dict[str, Any]) -> dict[str, Any] | None:
    """旧形态 → V2.5 形态；已是 V2.5 形态返回 None（跳过写库）。"""
    changed = False
    out = dict(cfg)
    if out.get("coverage_mode") is None:
        quantity_tendency = out.pop("quantity_tendency", None)
        out["coverage_mode"] = (
            quantity_tendency if quantity_tendency in _COVERAGE_MODES else DEFAULT_COVERAGE_MODE
        )
        changed = True
    ratio = out.get("difficulty_ratio")
    if not isinstance(ratio, dict):
        out["difficulty_ratio"] = dict(DEFAULT_DIFFICULTY_RATIO)
        changed = True
    elif "application" in ratio or any(isinstance(v, float) for v in ratio.values()):
        out["difficulty_ratio"] = _ratio_from_legacy(ratio)
        changed = True
    if "custom_requirements" not in out:
        out["custom_requirements"] = None
        changed = True
    return out if changed else None


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT task_id, generation_config FROM tasks")).fetchall()
    updated = 0
    for task_id, raw in rows:
        migrated = _migrate_generation_config(json.loads(raw))
        if migrated is None:
            continue
        bind.execute(
            sa.text("UPDATE tasks SET generation_config = :cfg WHERE task_id = :task_id"),
            {"cfg": json.dumps(migrated, ensure_ascii=False), "task_id": task_id},
        )
        updated += 1
    logger.info("V2.5 generation_config 回填：%d/%d 行已转换", updated, len(rows))


def downgrade() -> None:
    raise RuntimeError("迁移不可逆：generation_config 已转为 V2.5 形态，回退仅限恢复升级前备份")
