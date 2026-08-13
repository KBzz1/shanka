"""services.api_key：Key 保存/状态/覆盖规则/脱敏（structure-contract 6.2；database-design 2.2）。

覆盖规则：仅 AVAILABLE 落库/覆盖（INVALID/INSUFFICIENT_BALANCE 不保存不覆盖——6.2 旧有效 Key 保护）；
get_status 只返回 DB 状态（不解密不重校验——校验是写路径动作；V4 生成时若 Key 失效 chat 抛 API_KEY_UNAVAILABLE）。
明文 Key 只存在于调用栈（handler → service → adapter/crypto），不落库不落日志。

P4-4（原 plan Task 5 前移）：Key 归属切 user 域——写入/查询一律按 user_id（新行 user_id 非空、
device_id NULL，满足 CHECK 双非空；旧 device 域行按 D-06 无访问路径）。ApiKey 的 ORM mapper 身份键
仍为过渡 device_id（P3 遗留，Task 5 移除），用户域行对 ORM 不可见——本服务全部走 Core 直写/查询
（insert/update/select 列投影，不经 ORM 实例路径）。
"""

from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from infra.db.models import ApiKey
from infra.llm.crypto import encrypt_key
from infra.llm.deepseek import DeepSeekClient


def save_key(
    session: Session,
    *,
    user_id: str,
    api_key: str,
    encryption_key: bytes,
    client: DeepSeekClient,
    now: str,
) -> dict[str, Any]:
    status = client.validate_key(api_key)
    if status != "AVAILABLE":
        # 校验失败不落库不覆盖（6.2）；返回状态供前端展示
        return {"status": status, "masked_key": masked(api_key), "updated_at": now}
    encrypted = encrypt_key(api_key, encryption_key)
    exists = session.execute(select(ApiKey.user_id).where(ApiKey.user_id == user_id)).first()
    if exists is None:
        # 用户域 Core 直写（device_id NULL——新写入不再生成 device_id，DESIGN §5.2）
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                device_id=None,
                encrypted_key=encrypted,
                status=status,
                masked_key=masked(api_key),
                updated_at=now,
            )
        )
    else:
        # Core 条件更新（synchronize_session=False，P3 同款——ORM 对用户域行 UPDATE 抛
        # FlushError）；业务语义不变（同 user_id 覆盖加密 Key/状态，6.2 覆盖规则）
        session.execute(
            update(ApiKey)
            .where(ApiKey.user_id == user_id)
            .values(
                encrypted_key=encrypted,
                status=status,
                masked_key=masked(api_key),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    return {"status": status, "masked_key": masked(api_key), "updated_at": now}


def get_status(session: Session, *, user_id: str, encryption_key: bytes) -> dict[str, Any]:
    """用户域查询（列投影 Core select——用户域行对 ORM 不可见）；旧 device 域行不可见（D-06）。"""
    row = session.execute(
        select(ApiKey.status, ApiKey.masked_key, ApiKey.updated_at).where(ApiKey.user_id == user_id)
    ).first()
    if row is None:
        return {"status": "UNKNOWN", "masked_key": "", "updated_at": None}
    return {"status": row.status, "masked_key": row.masked_key, "updated_at": row.updated_at}


def masked(api_key: str) -> str:
    """脱敏展示（规则唯一：sk-**** + 末 4 位；len<=4 全掩码）。"""
    if len(api_key) <= 4:
        return "sk-****"
    return f"sk-****{api_key[-4:]}"
