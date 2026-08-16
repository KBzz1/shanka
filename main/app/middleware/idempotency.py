"""Idempotency-Key 幂等原语（structure-contract 1.3；database-design 2.12；红线 3 于 app/middleware 统一）。

execute_idempotent 由写接口 handler 在请求级 session 内调用：
- 首次：执行 fn(session) → INSERT 幂等记录（response_status/response_body/request_body_hash），
  与业务副作用同一事务（调用方 commit；失败回滚同时释放占位）。
- 重复：同键同 body → 重放首次成功响应（不执行业务）；同键异 body → 409 IDEMPOTENCY_CONFLICT。
- 并发：写事务以显式 `BEGIN IMMEDIATE` 开始（database-design §0/3 语义保留在写路径；
  2026-08-16 起 engine 级全局 BEGIN IMMEDIATE 已移除，读事务不再抢写锁）——同键并发写
  由 DB 级写锁串行化（跨进程同样成立），保证业务副作用仅一次（AC-05/AC-10）；
  调用方已开始事务的降级场景由 flush 唯一约束冲突分支兜底（回滚后重读 → 重放）。
- V2.2：幂等域 = user（P4-3 切换）；新行只写 user_id。
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
    user_id: str,
    path: str,
    idempotency_key: str,
    request_body_hash: str,
    fn: F,
) -> tuple[bool, int, dict[str, Any]]:
    """执行或重放幂等操作。返回 (是否重放, status, body)。

    调用方负责事务：成功后 commit（幂等记录与副作用同事务）；失败 rollback。
    写事务以显式 BEGIN IMMEDIATE 开始（DB 级写锁串行化同键并发写，跨进程成立）；
    调用方已开始事务（先读/先写）的降级场景由 flush 唯一约束冲突分支兜底。
    """
    if session.in_transaction():
        # 调用方已开始事务（SQLAlchemy Transaction 已激活）→ 无法升级为 IMMEDIATE，
        # 降级为普通事务，并发单效果依赖 flush 唯一约束冲突分支兜底。
        logger.debug(
            "idempotency: transaction already open, degraded claim for key=%s", idempotency_key
        )
    else:
        conn = session.connection()
        conn.exec_driver_sql("BEGIN IMMEDIATE")
    return _execute_idempotent_claimed(
        session,
        user_id=user_id,
        path=path,
        idempotency_key=idempotency_key,
        request_body_hash=request_body_hash,
        fn=fn,
    )


def _execute_idempotent_claimed[F: Callable[[Session], tuple[int, dict[str, Any]]]](
    session: Session,
    *,
    user_id: str,
    path: str,
    idempotency_key: str,
    request_body_hash: str,
    fn: F,
) -> tuple[bool, int, dict[str, Any]]:
    existing = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
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
    if not 200 <= status < 300:
        # 仅记录成功(2xx)响应：非 2xx 不落幂等记录，同键重试重新执行（契约 1.3/2.12）
        return False, status, body

    record = IdempotencyKey(
        user_id=user_id,
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
        # 跨进程并发占位冲突：回滚后重读重放（业务副作用随事务回滚，不重复）
        session.rollback()
        existing = session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id,
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
