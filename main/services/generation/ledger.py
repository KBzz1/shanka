"""ledger.py：llm_call_attempts 调用账本（spec §9；调用前 STARTED 占位，重试/上限/成本权威）。

- 任何外部 chat 调用必须先有已提交的 STARTED 行；调用返回并完成校验后，业务写入与
  账本终态在同一事务提交（提交失败时保留 STARTED，恢复后按 UNKNOWN 处理）。
- 重试判定：operation_key 的 STARTED/SUCCESS/FAILED/UNKNOWN 尝试总数 = 预算消耗
  （attempt_count）；孤儿 STARTED 经 mark_stale_unknown 转 UNKNOWN 仍计数。
- 唯一约束 (scope_type, scope_id, stage, operation_key, attempt_no)：冲突 = 同操作
  重复尝试，409 语义抛 AppError(IDEMPOTENCY_CONFLICT)（捕获 IntegrityError →
  session.rollback() 后转换）。
- 时间：services 层约定显式 now（format_utc 字符串，database-design 0）；缺省用
  SystemClock（app 层同款）。
- 红线 4：normalized_result 仅保存调用方传入的已验证规范化 units JSON；本层不写
  完整 Prompt、原文或原始模型响应。
"""

import uuid
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import LlmCallAttempt
from infra.db.session import format_utc

STARTED = "STARTED"
SUCCESS = "SUCCESS"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
_SCORING = "SCORING"
# 预算口径：四种状态全部算作已尝试（§9 重试判定）
_COUNTED_STATUSES = (STARTED, SUCCESS, FAILED, UNKNOWN)
# DeepSeek usage 键 → 账本列（usage 原样映射，成本口径唯一来源）
_USAGE_KEYS = {
    "prompt_cache_hit_tokens": "cache_hit",
    "prompt_cache_miss_tokens": "cache_miss",
    "completion_tokens": "output_tokens",
}


def _now(now: str | None) -> str:
    return now if now is not None else format_utc(SystemClock().now_utc())


def create_attempt(
    session: Session,
    *,
    device_id: str,
    scope_type: str,
    scope_id: str,
    task_id: str | None,
    stage: str,
    operation_key: str,
    input_fingerprint: str,
    attempt_no: int,
    model: str,
    prompt_name: str,
    prompt_version: str,
    schema_name: str | None = None,
    schema_version: str | None = None,
    rubric_version: str | None = None,
    now: str | None = None,
) -> LlmCallAttempt:
    """调用前 STARTED 占位：INSERT 后 flush；唯一约束冲突 → AppError(IDEMPOTENCY_CONFLICT)。

    同一 (scope_type, scope_id, stage, operation_key, attempt_no) 只允许一次尝试；
    冲突时回滚本事务（含前序占位），由调用方决定是否重试新 attempt_no。
    """
    attempt = LlmCallAttempt(
        call_id=str(uuid.uuid4()),
        device_id=device_id,
        scope_type=scope_type,
        scope_id=scope_id,
        task_id=task_id,
        stage=stage,
        operation_key=operation_key,
        attempt_no=attempt_no,
        input_fingerprint=input_fingerprint,
        model=model,
        prompt_name=prompt_name,
        prompt_version=prompt_version,
        schema_name=schema_name,
        schema_version=schema_version,
        rubric_version=rubric_version,
        status=STARTED,
        created_at=_now(now),
    )
    session.add(attempt)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "该操作已存在调用尝试（重复 attempt_no），请使用新 attempt_no 重试",
        ) from None
    return attempt


def finish_success(
    session: Session,
    attempt: LlmCallAttempt,
    *,
    usage: dict[str, int],
    http_status: int,
    duration_ms: int,
    normalized_result: str | None = None,
    now: str | None = None,
) -> None:
    """调用成功终态：usage 的 prompt_cache_hit/miss/completion → cache_hit/miss/output_tokens。

    仅本函数写入 usage（成本口径唯一来源）；终态与业务结果由调用方在同一事务提交。
    """
    attempt.status = SUCCESS
    for usage_key, column in _USAGE_KEYS.items():
        setattr(attempt, column, usage[usage_key])
    attempt.http_status = http_status
    attempt.duration_ms = duration_ms
    attempt.normalized_result = normalized_result
    attempt.finished_at = _now(now)


def finish_failed(
    session: Session, attempt: LlmCallAttempt, *, error_code: str, now: str | None = None
) -> None:
    """调用失败终态：FAILED + error_code（不写 usage）。"""
    attempt.status = FAILED
    attempt.error_code = error_code
    attempt.finished_at = _now(now)


def mark_stale_unknown(
    session: Session, *, task_id: str, stage: str, now: str | None = None
) -> int:
    """孤儿恢复（§6.2 CAS2）：该任务+阶段的遗留 STARTED 转 UNKNOWN，返回受影响行数。

    UNKNOWN 仍计入预算（attempt_count 含 UNKNOWN），防止重试预算被孤儿行无限放大。
    """
    result = cast(
        CursorResult[Any],
        session.execute(
            update(LlmCallAttempt)
            .where(
                LlmCallAttempt.task_id == task_id,
                LlmCallAttempt.stage == stage,
                LlmCallAttempt.status == STARTED,
            )
            .values(status=UNKNOWN, finished_at=_now(now))
        ),
    )
    return result.rowcount


def attempt_count(session: Session, *, task_id: str, stage: str, operation_key: str) -> int:
    """预算口径：该 operation_key 的 STARTED/SUCCESS/FAILED/UNKNOWN 尝试总数（§9 重试判定）。"""
    total = session.scalar(
        select(func.count())
        .select_from(LlmCallAttempt)
        .where(
            LlmCallAttempt.task_id == task_id,
            LlmCallAttempt.stage == stage,
            LlmCallAttempt.operation_key == operation_key,
            LlmCallAttempt.status.in_(_COUNTED_STATUSES),
        )
    )
    return int(total or 0)


def find_success_result(
    session: Session, *, task_id: str, stage: str, operation_key: str, input_fingerprint: str
) -> str | None:
    """SUCCESS 行的 normalized_result（规划恢复用；§9：同 key+fingerprint 最多一个 SUCCESS）。"""
    return session.scalar(
        select(LlmCallAttempt.normalized_result)
        .where(
            LlmCallAttempt.task_id == task_id,
            LlmCallAttempt.stage == stage,
            LlmCallAttempt.operation_key == operation_key,
            LlmCallAttempt.input_fingerprint == input_fingerprint,
            LlmCallAttempt.status == SUCCESS,
        )
        .limit(1)
    )


def scoring_attempt_total(session: Session, *, task_id: str) -> int:
    """Scoring 上限口径（§9）：该任务 stage=SCORING 的全部尝试数（各状态都算）。"""
    total = session.scalar(
        select(func.count())
        .select_from(LlmCallAttempt)
        .where(
            LlmCallAttempt.task_id == task_id,
            LlmCallAttempt.stage == _SCORING,
        )
    )
    return int(total or 0)


def task_token_totals(session: Session, *, task_id: str) -> dict[str, dict[str, int]]:
    """按 stage 汇总 cache_hit/cache_miss/output_tokens（成本口径唯一来源，§9）。

    返回 `{stage: {"cache_hit": N, "cache_miss": N, "output_tokens": N}}`；
    仅 SUCCESS 行带 usage，NULL 由 SUM/COALESCE 归 0，因此与只计 SUCCESS 等价。
    注意：plan 文档注解为 dict[str, int]，实际按描述返回三列分项（见任务报告）。
    """
    rows = session.execute(
        select(
            LlmCallAttempt.stage,
            func.coalesce(func.sum(LlmCallAttempt.cache_hit), 0),
            func.coalesce(func.sum(LlmCallAttempt.cache_miss), 0),
            func.coalesce(func.sum(LlmCallAttempt.output_tokens), 0),
        )
        .where(LlmCallAttempt.task_id == task_id)
        .group_by(LlmCallAttempt.stage)
    )
    return {
        stage: {
            "cache_hit": int(hit),
            "cache_miss": int(miss),
            "output_tokens": int(out),
        }
        for stage, hit, miss, out in rows
    }
