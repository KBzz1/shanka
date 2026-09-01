"""services.projects.versioning：处理状态终态跃迁的项目版本刷新单一入口（契约 4.5，V25-D-34）。

服务端不变式：客户端可见的异步处理终态跃迁（PDF 解析发布、任务终态）必须刷新拥有者项目的
version/updated_at，使以版本为失效信号的客户端缓存能感知变化。条件 UPDATE 保证行不存在或
并发删除时静默跳过——观察方下一次列表读自然不再看到该行。
"""

from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from infra.db.models import LearningProject


def bump_project_version(session: Session, *, project_id: str, now: str) -> bool:
    """刷新项目 version/updated_at；项目行已删除时返回 False（无害跳过）。"""
    result = cast(
        CursorResult[Any],
        session.execute(
            update(LearningProject)
            .where(LearningProject.project_id == project_id)
            .values(version=now, updated_at=now)
        ),
    )
    return result.rowcount == 1
