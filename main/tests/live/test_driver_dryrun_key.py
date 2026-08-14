"""driver dry-run Key 直插判别测试（P4-4 review MAJOR-1 回归锁定）。

回归背景（V2.1 历史）：T4 前 dry-run Key 直插走 ORM device 域行；T4 后 headers 无
X-Device-ID、devices 无注册行（FK 无匹配 → IntegrityError 崩溃），且 create_task/executor
按 user_id 查 Key（device 域行不可见 → 全单元 422 API_KEY_NOT_SET）。本测试锁定修复：
直插行 user_id 非空（V2.3 起 api_keys 已无 device_id 列）+ user 域服务查询可见。
"""

import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text

from infra.db.models import ApiKey, User
from infra.db.session import format_utc
from services.api_key.service import get_status, masked
from tests.live.driver import _DRY_RUN_KEY, _save_dry_run_key, migrate_db


def test_dry_run_key_insert_user_domain(tmp_path: Path) -> None:
    """直插行 user_id 非空（api_keys 无 device_id 列）+ get_status 按 user_id 可见。"""
    db_path = tmp_path / "driver.db"
    session_factory = migrate_db(db_path)
    user_id = str(uuid.uuid4())
    enc_key = secrets.token_bytes(32)
    now = format_utc(datetime.now(UTC))
    with session_factory() as session:
        session.add(
            User(
                user_id=user_id,
                username="live-driver",
                email="live-driver@local.test",  # 测试假凭据，与 driver 注册同款（V2.4 email 登录键）
                password_hash="live-driver-pass-1",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    with session_factory() as session:
        # FK 强制开启（engine 级 PRAGMA）：直插成功本身即 FK 校验通过
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        _save_dry_run_key(session, user_id=user_id, api_key=_DRY_RUN_KEY, encryption_key=enc_key)
        session.commit()

    with session_factory() as session:
        # V2.3：api_keys 表无 device_id 列（随不可逆迁移删除）
        cols = {row[1] for row in session.execute(text("PRAGMA table_info(api_keys)"))}
        assert "device_id" not in cols
        row = session.execute(select(ApiKey.user_id, ApiKey.status)).one()
        assert row.user_id == user_id
        assert row.status == "AVAILABLE"
        status = get_status(session, user_id=user_id, encryption_key=enc_key)
        assert status["status"] == "AVAILABLE"
        assert status["masked_key"] == masked(_DRY_RUN_KEY)
