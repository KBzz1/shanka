"""账号用例（DESIGN §4.2/§4.3）：注册/登录/登出/当前用户/principal 解析。

- register/login：校验 → 规范化 → dummy 或 verify → User/AuthSession INSERT → token 生成；
  email 冲突（含并发唯一约束兜底）→ 409 EMAIL_TAKEN；登录失败统一 401 INVALID_CREDENTIALS
  （邮箱不存在先执行固定 dummy 校验抹平时序差；损坏 PHC 哈希 catch InvalidHashError 绝不 500）。
- logout：条件更新 revoked_at（已撤销/重放不再执行副作用），幂等。
- resolve_principal：只查 auth_sessions（行含 user_id 无需 JOIN），撤销/过期 → None，供中间件。
- login email 桶限流（P4-3→V2.4 桶键改 email）：RateLimiter 共享实例由 handler 从 app.state 注入
  （body 于 BodyCapture 内层，middleware 不可读——裁决）；检查在规范化+校验后，
  超限 → RATE_LIMITED（handler 捕获后 429 + Retry-After）。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from argon2.exceptions import InvalidHashError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # 仅类型（分层不允许 services 运行时依赖 app——运行时鸭子类型调用 check）
    from app.middleware.rate_limit import RateLimiter

from app.errors import AppError, ErrorCode
from domain.auth import AuthPrincipal
from infra.clock import SystemClock
from infra.db.models import AuthSession, User
from infra.db.session import format_utc
from services.auth.password import hash_password, verify_dummy, verify_password
from services.auth.tokens import generate_session_token, hash_session_token

_USERNAME_RE = re.compile(r"^[\w.\-]{1,24}$")  # Unicode 字母数字（含中文）/._-，1-24 位
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")  # 宽松：含 @、无空白；长度上限由 schema 254 保证


def _normalize_username(username: str) -> str:
    return username.strip()  # 展示名：只 trim，不再强制小写


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise AppError(ErrorCode.VALIDATION_ERROR, "用户名须为 1-24 位中文/字母/数字/._-")


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.fullmatch(email):
        raise AppError(ErrorCode.VALIDATION_ERROR, "邮箱格式不正确")


def _validate_password(password: str) -> None:
    if len(password) < 8 or len(password) > 128:
        raise AppError(ErrorCode.VALIDATION_ERROR, "密码须为 8-128 个字符")


def _invalid_credentials() -> AppError:
    return AppError(ErrorCode.INVALID_CREDENTIALS, "邮箱或密码错误")


def _verify_or_false(password: str, password_hash: str) -> bool:
    """verify_password 兜底：损坏 PHC 哈希（InvalidHashError）视为校验失败，绝不 500。"""
    try:
        return verify_password(password, password_hash)
    except InvalidHashError:
        return False


def _create_session(
    session: Session, *, user_id: str, now: datetime, ttl_days: int
) -> tuple[str, str]:
    """新建 AuthSession 行，返回 (明文 token, expires_at 字符串)。"""
    token = generate_session_token()
    session_id = str(uuid.uuid4())
    session.add(
        AuthSession(
            session_id=session_id,
            user_id=user_id,
            token_hash=hash_session_token(token),
            created_at=format_utc(now),
            expires_at=format_utc(now + timedelta(days=ttl_days)),
            revoked_at=None,
        )
    )
    return token, format_utc(now + timedelta(days=ttl_days))


def renew_session_if_due(
    session: Session, *, session_id: str, now: datetime, ttl_days: int
) -> None:
    """滑动续期（V2.4）：剩余有效期不足 1 天时延长到 now + ttl_days。

    节流：仅在 expires_at < now + (ttl_days - 1) 天 时写库 → 每会话每天至多一次
    UPDATE；活跃用户永不过期，连续 ttl_days 天无请求的会话仍自然过期。
    调用前提：resolve_principal 已确认会话未撤销未过期。
    """
    threshold = format_utc(now + timedelta(days=ttl_days - 1))
    session.execute(
        update(AuthSession)
        .where(AuthSession.session_id == session_id, AuthSession.expires_at < threshold)
        .values(expires_at=format_utc(now + timedelta(days=ttl_days)))
    )


def register_user(
    session: Session,
    *,
    username: str,
    email: str,
    password: str,
    now: datetime,
    ttl_days: int,
) -> tuple[dict[str, str], str, str]:
    """注册：创建用户 + 首个会话；返回 (user_dict, access_token, expires_at)。"""
    normalized_username = _normalize_username(username)
    normalized_email = _normalize_email(email)
    _validate_username(normalized_username)
    _validate_email(normalized_email)
    _validate_password(password)
    existing = session.scalar(select(User).where(User.email == normalized_email))
    if existing is not None:
        raise AppError(ErrorCode.EMAIL_TAKEN, "邮箱已被占用")
    user_id = str(uuid.uuid4())
    created_at = format_utc(now)
    session.add(
        User(
            user_id=user_id,
            username=normalized_username,
            email=normalized_email,
            password_hash=hash_password(password),
            created_at=created_at,
            updated_at=created_at,
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        # 并发注册同 email：UNIQUE 约束兜底 → 409
        raise AppError(ErrorCode.EMAIL_TAKEN, "邮箱已被占用") from exc
    token, expires_at = _create_session(session, user_id=user_id, now=now, ttl_days=ttl_days)
    user_dict = {"user_id": user_id, "username": normalized_username, "created_at": created_at}
    return user_dict, token, expires_at


def login_user(
    session: Session,
    *,
    email: str,
    password: str,
    now: datetime,
    ttl_days: int,
    email_limiter: RateLimiter,
) -> tuple[dict[str, str], str, str]:
    """登录：校验凭据 + 新建会话；失败统一 INVALID_CREDENTIALS（不暴露用户存在性）。

    email_limiter：login email 桶（P4-3→V2.4 桶键改 email）——规范化后先 check，
    超限抛 RATE_LIMITED；成功与失败登录均计入桶。
    """
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)
    allowed, _retry_after = email_limiter.check(normalized_email)
    if not allowed:
        raise AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试")
    user = session.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        verify_dummy(password)  # 固定 dummy 校验抹平账号存在性时序差（DESIGN §4.2）
        raise _invalid_credentials()
    if not _verify_or_false(password, user.password_hash):
        raise _invalid_credentials()
    token, expires_at = _create_session(session, user_id=user.user_id, now=now, ttl_days=ttl_days)
    user_dict = {
        "user_id": user.user_id,
        "username": user.username,
        "created_at": user.created_at,
    }
    return user_dict, token, expires_at


def logout_session(session: Session, *, session_id: str, now: datetime) -> None:
    """撤销会话：条件更新（已撤销/不存在的 session 不产生副作用，幂等）。"""
    session.execute(
        update(AuthSession)
        .where(AuthSession.session_id == session_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=format_utc(now))
    )


def get_current_user(session: Session, *, session_id: str) -> dict[str, str]:
    """当前用户最小资料：session 存在且未撤销且未过期 → user_dict；否则 AUTH_INVALID。"""
    now = format_utc(SystemClock().now_utc())
    row = session.execute(
        select(AuthSession, User)
        .join(User, AuthSession.user_id == User.user_id)
        .where(AuthSession.session_id == session_id)
    ).first()
    if row is None:
        raise AppError(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")
    auth_session, user = row
    if auth_session.revoked_at is not None or auth_session.expires_at <= now:
        raise AppError(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")
    return {
        "user_id": user.user_id,
        "username": user.username,
        "created_at": user.created_at,
    }


def resolve_principal(session: Session, *, token_hash: str, now: str) -> AuthPrincipal | None:
    """按 token 摘要解析主体（中间件用）：撤销/过期 → None。

    只查 auth_sessions——行内即含 user_id，无需 JOIN users（DESIGN §4.3）。
    """
    row = session.scalar(select(AuthSession).where(AuthSession.token_hash == token_hash))
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    return AuthPrincipal(user_id=row.user_id, session_id=row.session_id)
