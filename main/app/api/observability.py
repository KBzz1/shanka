"""质量聚合观测（structure-contract 6.10；openapi /observability/quality-summary）。

按当前 device 隔离聚合（跨设备聚合留给运营后台）：SQL 拉取窗口内批次（join Task），
按 group_by 分组聚合——Rubric 各维平均（批次卡片经 generated_item_ids ↔
Card.generation_item_id 关联，各维度只以对应字段非 NULL 的卡为分母，NULL 不计 0 分）、
覆盖/重复率均值（SKIPPED 批次 coverage=0 计入分母）、任务完成率（COMPLETED/总数）、
成本汇总（8.4 价格常量；Batch token 列为生成阶段兼容投影，估算标注
scope=generation-stage-only，不引入账本双计）。
difficulty 分组键 = Batch.generation_unit_id → KnowledgePoint.target_difficulty
（单元缺失/锚定缺失 → unknown）；分组按批次 rubric_version 拆子组（卡无
rubric_version 列，经批次归属取版本）。
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from infra.clock import SystemClock
from infra.db.models import Batch, Card, KnowledgePoint, Task
from infra.db.session import format_utc, get_db_session
from services.generation.cost import estimate_cost_by_kind

router = APIRouter(prefix="/observability", tags=["observability"])

_UNKNOWN = "unknown"
_COST_SCOPE = "generation-stage-only"  # Batch token 列为生成阶段兼容投影（spec §8）
_CARD_QUERY_CHUNK = 500  # SQLite 变量数上限兜底（IN 分批）


@dataclass
class _Group:
    """单分组聚合累加器（模块内部，不对外暴露）。

    各评分维度独立 sum/n（NULL 不计 0 分、不进分母）；card_count = eligible
    （经批次归属的卡数）；scored_card_count = rubric_total_score 非 NULL。
    """

    task_ids: set[str] = field(default_factory=set)
    completed_task_ids: set[str] = field(default_factory=set)
    card_count: int = 0
    evidence_sum: float = 0.0
    evidence_n: int = 0
    correctness_sum: float = 0.0
    correctness_n: int = 0
    difficulty_sum: float = 0.0
    difficulty_n: int = 0
    learning_sum: float = 0.0
    learning_n: int = 0
    scored_card_count: int = 0
    coverage_sum: float = 0.0
    coverage_n: int = 0
    duplicate_sum: float = 0.0
    duplicate_n: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0


@router.get("/quality-summary", response_model=dict[str, object])
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
        user_id=request.state.principal.user_id,
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
    user_id: str,
    group_by: Literal["model", "pdf", "difficulty"],
    days: int,
    now: datetime,
) -> list[dict[str, Any]]:
    cutoff = format_utc(now - timedelta(days=days))
    rows = session.execute(
        select(Batch, Task.file_id, Task.status)
        .join(Task, Task.task_id == Batch.task_id)
        .where(Task.user_id == user_id, Batch.created_at >= cutoff)
    ).all()
    difficulty_by_unit = _unit_difficulties(session, rows)
    groups: dict[tuple[str, str | None], _Group] = {}
    owner: dict[str, tuple[str, str | None]] = {}  # generation_item_id → 子组键（卡片经此归属）
    for row in rows:
        batch: Batch = row.Batch
        key = _group_key(batch, row.file_id, group_by, difficulty_by_unit)
        sub_key = (key, batch.rubric_version)  # 版本拆子组：卡经批次归属取版本（spec §8）
        g = groups.setdefault(sub_key, _Group())
        g.task_ids.add(batch.task_id)
        if row.status == "COMPLETED":
            g.completed_task_ids.add(batch.task_id)
        for item_id in _parse_list(batch.generated_item_ids):
            owner[item_id] = sub_key
        if batch.coverage_rate is not None:  # SKIPPED 批次 coverage=0 计入分母（spec §8）
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
            if card.evidence_score is not None:  # 各维度独立分母；NULL 不计 0 分
                g.evidence_sum += card.evidence_score
                g.evidence_n += 1
            if card.correctness_score is not None:
                g.correctness_sum += card.correctness_score
                g.correctness_n += 1
            if card.difficulty_score is not None:
                g.difficulty_sum += card.difficulty_score
                g.difficulty_n += 1
            if card.learning_value_score is not None:
                g.learning_sum += card.learning_value_score
                g.learning_n += 1
            if card.rubric_total_score is not None:  # "已被评分"口径（spec §8）
                g.scored_card_count += 1

    effective_date = now.date().isoformat()
    out: list[dict[str, Any]] = []
    for sub_key in sorted(groups, key=lambda k: (k[0], k[1] or "")):  # None 版本排前（确定性）
        key, rubric_version = sub_key
        g = groups[sub_key]
        eligible = g.card_count
        out.append(
            {
                "key": key,
                "rubric_version": rubric_version,
                "card_count": eligible,
                "eligible_card_count": eligible,
                "scored_card_count": g.scored_card_count,
                "sampling_rate": (
                    round(g.scored_card_count / eligible, 6) if eligible else None
                ),  # 分母 0 → JSON null
                "evidence_avg": _avg(g.evidence_sum, g.evidence_n),
                "correctness_avg": _avg(g.correctness_sum, g.correctness_n),
                "difficulty_avg": _avg(g.difficulty_sum, g.difficulty_n),
                "learning_value_avg": _avg(g.learning_sum, g.learning_n),
                "coverage_avg": _avg(g.coverage_sum, g.coverage_n),
                "duplicate_avg": _avg(g.duplicate_sum, g.duplicate_n),
                "task_completion_rate": _ratio(len(g.completed_task_ids), len(g.task_ids)),
                "cost_estimate": {
                    **estimate_cost_by_kind(
                        cache_hit_tokens=g.cache_hit_tokens,
                        cache_miss_tokens=g.cache_miss_tokens,
                        output_tokens=g.output_tokens,
                        effective_date=effective_date,
                    ),
                    "scope": _COST_SCOPE,
                },
            }
        )
    return out


def _unit_difficulties(session: Session, rows: Sequence[Any]) -> dict[str, str]:
    """批次 → 单元 target_difficulty 预取映射（difficulty 归因经 unit 锚定，spec §8）。

    与卡片查询同款按 _CARD_QUERY_CHUNK 分块（SQLite 变量数上限兜底：窗口内单元数
    无上限约束，单次大 IN 会触发 "too many SQL variables"）。
    """
    unit_ids = {row.Batch.generation_unit_id for row in rows}
    unit_ids.discard(None)
    if not unit_ids:
        return {}
    by_id: dict[str, str] = {}
    ordered = sorted(unit_ids)
    for i in range(0, len(ordered), _CARD_QUERY_CHUNK):
        chunk = ordered[i : i + _CARD_QUERY_CHUNK]
        for unit in session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.knowledge_point_id.in_(chunk))
        ).all():
            by_id[unit.knowledge_point_id] = unit.target_difficulty or _UNKNOWN
    return by_id


def _group_key(
    batch: Batch,
    file_id: str | None,
    group_by: str,
    difficulty_by_unit: dict[str, str],
) -> str:
    """分组键：model → batch.model；pdf → 任务所属 PDF file_id；difficulty →
    generation_unit_id → 单元 target_difficulty（NULL/缺失 → unknown）。"""
    if group_by == "model":
        return batch.model or _UNKNOWN
    if group_by == "pdf":
        return file_id or _UNKNOWN
    if batch.generation_unit_id is None:
        return _UNKNOWN
    return difficulty_by_unit.get(batch.generation_unit_id, _UNKNOWN)


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
