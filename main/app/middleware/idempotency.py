"""Idempotency-Key 幂等原语（structure-contract 1.3；database-design 2.12；红线 3 于 app/middleware 统一）。

execute_idempotent 由写接口 handler 在请求级 session 内调用：
- 首次：执行 fn(session) → INSERT 幂等记录（response_status/response_body/request_body_hash），
  与业务副作用同一事务（调用方 commit；失败回滚同时释放占位）。
- 重复：同键同 body → 重放首次成功响应（不执行业务）；同键异 body → 409 IDEMPOTENCY_CONFLICT。
- 并发：唯一约束 (device_id, path, idempotency_key) 抢占；后到事务（BEGIN IMMEDIATE 串行化）回滚
  后重读 → 重放，保证业务副作用仅一次（AC-05/AC-10）。
"""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import IdempotencyKey
from infra.db.session import format_utc

logger = logging.getLogger(__name__)


def request_body_hash(data: bytes) -> str:
    """请求体 SHA-256 摘要（hex），幂等 body 比对载体（database-design 2.12）。"""
    return hashlib.sha256(data).hexdigest()


def get_idempotency_key(request: Request) -> str:
    """读 Idempotency-Key 请求头；缺失/非法 → VALIDATION_ERROR 400（写接口强制）。"""
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        raise AppError(ErrorCode.VALIDATION_ERROR, "写操作必须携带 Idempotency-Key")
    try:
        parsed = uuid.UUID(key)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Idempotency-Key 必须为 UUID") from exc
    return str(parsed)


def execute_idempotent[F: Callable[[Session], tuple[int, dict[str, Any]]]](
    session: Session,
    *,
    device_id: str,
    path: str,
    idempotency_key: str,
    request_body_hash: str,
    fn: F,
) -> tuple[bool, int, dict[str, Any]]:
    """执行或重放幂等操作。返回 (是否重放, status, body)。

    调用方负责事务：成功后 commit（幂等记录与副作用同事务）；失败 rollback。
    """
    existing = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.device_id == device_id,
            IdempotencyKey.path == path,
            IdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_body_hash != request_body_hash:
            logger.debug("idempotency conflict: body hash mismatch, key=%s", idempotency_key)
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 相同但请求体与首次不一致"
            )
        logger.debug("idempotent replay, key=%s", idempotency_key)
        return True, existing.response_status, json_loads_safe(existing.response_body)

    status, body = fn(session)

    record = IdempotencyKey(
        device_id=device_id,
        path=path,
        idempotency_key=idempotency_key,
        response_status=status,
        response_body=json_dumps_safe(body),
        request_body_hash=request_body_hash,
        created_at=format_utc(SystemClock().now_utc()),
    )
    session.add(record)
    try:
        session.flush()
    except Exception:
        # 并发占位冲突：回滚后重读重放（业务副作用随事务回滚，不重复）
        session.rollback()
        existing = session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.device_id == device_id,
                IdempotencyKey.path == path,
                IdempotencyKey.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        if existing.request_body_hash != request_body_hash:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 相同但请求体与首次不一致"
            )
        logger.debug("idempotent replay after concurrent claim, key=%s", idempotency_key)
        return True, existing.response_status, json_loads_safe(existing.response_body)

    return False, status, body


def json_dumps_safe(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def json_loads_safe(raw: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(raw))
