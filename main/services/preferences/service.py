"""账号偏好用例（structure-contract 3.15 / 6.1；openapi /preferences；V2.5 新增）。

- get_preferences：GET 时 get-or-create（首次访问落默认值行：BALANCED / 40/40/20 / 50 /
  Asia/Shanghai / current_project_id=null）；服务端校验均在写路径。
- update_preferences：部分更新 last-success-wins；比例（10% 档 0~100、合计 100、任一档可为 0、
  全 0 非法）与每日目标（10~200 的 10 倍数）非法 → INVALID_PREFERENCES（400，不得泛化
  VALIDATION_ERROR——Task 2 审查遗留 I-2）；IANA 时区非法 → INVALID_LEARNING_TIMEZONE；
  coverage_mode 非法 → VALIDATION_ERROR（7 章未注册偏好码，按结构非法处理）。
- current_project_id：uuid 或 null；项目存在性/归属由项目工作包（Task 4）承接，本任务只校验格式。
- 不返回密码/hash；API-key 字段不进入本资源载荷（6.1）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.enums import CoverageMode
from domain.preferences import (
    DEFAULT_COVERAGE_MODE,
    DEFAULT_DAILY_LEARNING_GOAL,
    DEFAULT_DIFFICULTY_RATIO,
    DEFAULT_LEARNING_TIMEZONE,
    DIFFICULTY_RATIO_TOTAL,
)
from infra.db.models import UserPreferences
from infra.db.session import format_utc


def _validate_ratio(ratio: dict[str, int]) -> None:
    values = (ratio["basic"], ratio["understanding"], ratio["deep_question"])
    if any(v < 0 or v > 100 or v % 10 != 0 for v in values):
        raise AppError(ErrorCode.INVALID_PREFERENCES, "比例须为 0~100 的 10% 整数档")
    if all(v == 0 for v in values):
        raise AppError(ErrorCode.INVALID_PREFERENCES, "比例全 0 为非法配置")
    if sum(values) != DIFFICULTY_RATIO_TOTAL:
        raise AppError(ErrorCode.INVALID_PREFERENCES, "三档比例合计必须为 100")


def _validate_daily_goal(goal: int) -> None:
    if goal < 10 or goal > 200 or goal % 10 != 0:
        raise AppError(ErrorCode.INVALID_PREFERENCES, "每日目标须为 10~200 的 10 倍数")


def _validate_timezone(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AppError(ErrorCode.INVALID_LEARNING_TIMEZONE, "无效的 IANA 时区") from exc


def _validate_coverage_mode(mode: str) -> None:
    if mode not in {m.value for m in CoverageMode}:
        raise AppError(ErrorCode.VALIDATION_ERROR, "非法 coverage_mode")


def _validate_project_id(project_id: str) -> None:
    try:
        uuid.UUID(project_id)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "current_project_id 须为 UUID") from exc


def _to_response(row: UserPreferences) -> dict[str, Any]:
    return {
        "default_coverage_mode": row.coverage_mode,
        "default_difficulty_ratio": {
            "basic": row.basic_ratio,
            "understanding": row.understanding_ratio,
            "deep_question": row.deep_question_ratio,
        },
        "daily_learning_goal": row.daily_goal,
        "learning_timezone": row.learning_timezone,
        "current_project_id": row.current_project_id,
        "updated_at": row.updated_at,
    }


def _get_or_create(session: Session, *, user_id: str, now: datetime) -> UserPreferences:
    """GET 语义 get-or-create：首次访问落默认值行（一用户一行）。"""
    row = session.get(UserPreferences, user_id)
    if row is not None:
        return row
    row = UserPreferences(
        user_id=user_id,
        coverage_mode=DEFAULT_COVERAGE_MODE,
        basic_ratio=DEFAULT_DIFFICULTY_RATIO["basic"],
        understanding_ratio=DEFAULT_DIFFICULTY_RATIO["understanding"],
        deep_question_ratio=DEFAULT_DIFFICULTY_RATIO["deep_question"],
        daily_goal=DEFAULT_DAILY_LEARNING_GOAL,
        learning_timezone=DEFAULT_LEARNING_TIMEZONE,
        current_project_id=None,
        updated_at=format_utc(now),
    )
    session.add(row)
    session.flush()
    return row


def get_preferences(session: Session, *, user_id: str, now: datetime) -> dict[str, Any]:
    return _to_response(_get_or_create(session, user_id=user_id, now=now))


def update_preferences(
    session: Session, *, user_id: str, payload: dict[str, Any], now: datetime
) -> dict[str, Any]:
    """部分更新（last-success-wins）：校验全部提供的字段 → 一次性落库并刷新 updated_at。

    校验先于任何写入：任一字段非法 → 整次更新拒绝（不部分生效），失败不落库。
    """
    row = _get_or_create(session, user_id=user_id, now=now)
    updates: dict[str, Any] = {}
    if payload.get("default_coverage_mode") is not None:
        mode = payload["default_coverage_mode"]
        _validate_coverage_mode(mode)
        updates["coverage_mode"] = mode
    if payload.get("default_difficulty_ratio") is not None:
        ratio = payload["default_difficulty_ratio"]
        _validate_ratio(ratio)
        updates["basic_ratio"] = ratio["basic"]
        updates["understanding_ratio"] = ratio["understanding"]
        updates["deep_question_ratio"] = ratio["deep_question"]
    if payload.get("daily_learning_goal") is not None:
        goal = payload["daily_learning_goal"]
        _validate_daily_goal(goal)
        updates["daily_goal"] = goal
    if payload.get("learning_timezone") is not None:
        tz = payload["learning_timezone"]
        _validate_timezone(tz)
        updates["learning_timezone"] = tz
    if "current_project_id" in payload:
        project_id = payload["current_project_id"]
        if project_id is not None:  # 显式 null = 清空当前项目（openapi type: [string, 'null']）
            _validate_project_id(project_id)
        updates["current_project_id"] = project_id
    if not updates:
        return _to_response(row)  # 空部分更新 = no-op（契约仅对 /auth/me 要求至少一个字段）
    updates["updated_at"] = format_utc(now)
    for column, value in updates.items():
        setattr(row, column, value)
    session.flush()
    return _to_response(row)
