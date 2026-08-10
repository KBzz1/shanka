"""质量聚合观测（structure-contract 6.10；openapi /observability/quality-summary）。

按当前 device 隔离聚合（跨设备聚合留给运营后台）：SQL 拉取窗口内批次（join Task），
按 group_by 分组聚合——Rubric 各维平均（批次卡片经 generated_item_ids ↔
Card.generation_item_id 关联）、覆盖/重复率均值、任务完成率（COMPLETED/总数）、
成本汇总（8.4 价格常量；历史 token 原样，估算只在聚合时算）。
difficulty 分组键 = 批次难度分布中计数最大档（同数取字典序最小，确定性）。
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.clock import SystemClock
from infra.db.models import Batch, Card, Task
from infra.db.session import format_utc, get_db_session
from services.generation.cost import estimate_cost_by_kind

router = APIRouter(prefix="/observability", tags=["observability"])

_UNKNOWN = "unknown"
_CARD_QUERY_CHUNK = 500  # SQLite 变量数上限兜底（IN 分批）


@dataclass
class _Group:
    """单分组聚合累加器（模块内部，不对外暴露）。"""

    task_ids: set[str] = field(default_factory=set)
    completed_task_ids: set[str] = field(default_factory=set)
    card_count: int = 0
    evidence_sum: float = 0.0
    correctness_sum: float = 0.0
    difficulty_sum: float = 0.0
    learning_sum: float = 0.0
    coverage_sum: float = 0.0
    coverage_n: int = 0
    duplicate_sum: float = 0.0
    duplicate_n: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0


@router.get("/quality-summary")
def quality_summary_endpoint(
    request: Request,
    session: Annotated[Session, Depends(get_db_session)],
    group_by: Literal["model", "pdf", "difficulty"] = "model",
    days: Annotated[int, Query(ge=1)] = 30,
) -> JSONResponse:
    """跨任务质量聚合（6.10）：Rubric 均分 / 覆盖重复率 / 任务完成率 / 成本汇总，按 device 隔离。"""
    now = SystemClock().now_utc()
    groups = _aggregate(
        session,
        device_id=request.state.device_id,
        group_by=group_by,
        days=days,
        now=now,
    )
    return JSONResponse(
        content={
            "group_by": group_by,
            "days": days,
            "generated_at": format_utc(now),
            "groups": groups,
        }
    )


def _aggregate(
    session: Session,
    *,
    device_id: str,
    group_by: Literal["model", "pdf", "difficulty"],
    days: int,
    now: datetime,
) -> list[dict[str, Any]]:
    cutoff = format_utc(now - timedelta(days=days))
    rows = session.execute(
        select(Batch, Task.file_id, Task.status)
        .join(Task, Task.task_id == Batch.task_id)
        .where(Task.device_id == device_id, Batch.created_at >= cutoff)
    ).all()
    groups: dict[str, _Group] = {}
    owner: dict[str, str] = {}  # generation_item_id → 分组键（卡片经此归属）
    for row in rows:
        batch: Batch = row.Batch
        key = _group_key(batch, row.file_id, group_by)
        g = groups.setdefault(key, _Group())
        g.task_ids.add(batch.task_id)
        if row.status == "COMPLETED":
            g.completed_task_ids.add(batch.task_id)
        for item_id in _parse_list(batch.generated_item_ids):
            owner[item_id] = key
        if batch.coverage_rate is not None:
            g.coverage_sum += batch.coverage_rate
            g.coverage_n += 1
        if batch.duplicate_rate is not None:
            g.duplicate_sum += batch.duplicate_rate
            g.duplicate_n += 1
        g.cache_hit_tokens += batch.cache_hit_tokens or 0
        g.cache_miss_tokens += batch.cache_miss_tokens or 0
        g.output_tokens += batch.output_tokens or 0

    item_ids = list(owner)
    for i in range(0, len(item_ids), _CARD_QUERY_CHUNK):
        chunk = item_ids[i : i + _CARD_QUERY_CHUNK]
        for card in session.scalars(select(Card).where(Card.generation_item_id.in_(chunk))).all():
            card_item_id = card.generation_item_id
            if card_item_id is None:  # owner 键均为非空（批次产出 id）
                continue
            g = groups[owner[card_item_id]]
            g.card_count += 1
            g.evidence_sum += card.evidence_score if card.evidence_score is not None else 0
            g.correctness_sum += card.correctness_score if card.correctness_score is not None else 0
            g.difficulty_sum += card.difficulty_score if card.difficulty_score is not None else 0
            g.learning_sum += (
                card.learning_value_score if card.learning_value_score is not None else 0
            )

    effective_date = now.date().isoformat()
    out: list[dict[str, Any]] = []
    for key in sorted(groups):
        g = groups[key]
        out.append(
            {
                "key": key,
                "card_count": g.card_count,
                "evidence_avg": _avg(g.evidence_sum, g.card_count),
                "correctness_avg": _avg(g.correctness_sum, g.card_count),
                "difficulty_avg": _avg(g.difficulty_sum, g.card_count),
                "learning_value_avg": _avg(g.learning_sum, g.card_count),
                "coverage_avg": _avg(g.coverage_sum, g.coverage_n),
                "duplicate_avg": _avg(g.duplicate_sum, g.duplicate_n),
                "task_completion_rate": _ratio(len(g.completed_task_ids), len(g.task_ids)),
                "cost_estimate": estimate_cost_by_kind(
                    cache_hit_tokens=g.cache_hit_tokens,
                    cache_miss_tokens=g.cache_miss_tokens,
                    output_tokens=g.output_tokens,
                    effective_date=effective_date,
                ),
            }
        )
    return out


def _group_key(batch: Batch, file_id: str | None, group_by: str) -> str:
    """分组键：model → batch.model；pdf → 任务所属 PDF file_id；difficulty → 分布计数最大档。"""
    if group_by == "model":
        return batch.model or _UNKNOWN
    if group_by == "pdf":
        return file_id or _UNKNOWN
    dist = _parse_dict(batch.difficulty_distribution)
    if not dist:
        return _UNKNOWN
    # 计数最大档；同数取字典序最小（确定性）
    return min(dist.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _avg(total: float, n: int) -> float:
    return round(total / n, 6) if n else 0.0


def _ratio(completed: int, total: int) -> float:
    return round(completed / total, 6) if total else 0.0


def _parse_list(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _parse_dict(raw: str | None) -> dict[str, int]:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}
