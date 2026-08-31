"""契约守卫（V2.5）：domain 枚举/常量 ↔ openapi.yaml 枚举 + structure-contract 状态机。

V2.5 落地点：
- 任务七态（DRAFT/SAMPLE_GENERATING/AWAITING_SAMPLE_CONFIRMATION/GENERATING/
  COMPLETED/FAILED/ABANDONED）与历史四态迁移映射（PENDING→DRAFT、RUNNING→GENERATING、
  CANCELLED→ABANDONED、PAUSED→FAILED + LEGACY_PAUSED_TASK 占位）；
- APPLICATION → DEEP_QUESTION 改名（openapi Difficulty 枚举 + 迁移映射常量）；
- STAGED/PUBLISHED 发布态与统一可见谓词（publication_state=PUBLISHED AND
  delete_batch_id IS NULL，契约 3.9）；
- 新增资源枚举（ProjectStatus/DeletionBatchStatus/RewritePreviewStatus/avatar_key）。
"""

from domain import (
    card,
    deletion_batch,
    enums,
    preferences,
    rewrite_preview,
    task,
)
from tests.contract.support import load_openapi


def _openapi_enum(schema_name: str, prop: str | None = None) -> set[str]:
    schema = load_openapi()["components"]["schemas"][schema_name]
    if prop:
        schema = schema["properties"][prop]
    return set(schema["enum"])


def test_task_status_matches_openapi_seven_states() -> None:
    expected = {
        "DRAFT",
        "SAMPLE_GENERATING",
        "AWAITING_SAMPLE_CONFIRMATION",
        "GENERATING",
        "COMPLETED",
        "FAILED",
        "ABANDONED",
    }
    assert set(enums.TaskStatus) == expected
    assert set(enums.TaskStatus) == _openapi_enum("TaskStatus")


def test_task_status_legacy_migration_map() -> None:
    """V2.5 七态迁移映射（database-design 7.3）：PENDING→DRAFT、RUNNING→GENERATING、
    CANCELLED→ABANDONED；PAUSED→FAILED + LEGACY_PAUSED_TASK 占位（禁止留下不可表达状态）。"""
    legacy_map = task.TASK_STATUS_V25_MIGRATION
    assert legacy_map == {
        "PENDING": "DRAFT",
        "RUNNING": "GENERATING",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
        "CANCELLED": "ABANDONED",
        "PAUSED": "FAILED",
    }
    # 映射值必须全部落在 V2.5 七态内（SAMPLE_GENERATING / AWAITING_SAMPLE_CONFIRMATION
    # 为 V2.5 新状态，无遗留来源，不在映射值域内）
    assert set(legacy_map.values()) <= set(enums.TaskStatus)
    assert "PAUSED" not in set(enums.TaskStatus)
    assert "PENDING" not in set(enums.TaskStatus)
    assert "RUNNING" not in set(enums.TaskStatus)
    assert "CANCELLED" not in set(enums.TaskStatus)
    assert task.LEGACY_PAUSED_TASK_ERROR_CODE == "LEGACY_PAUSED_TASK"


def test_difficulty_renamed_application_to_deep_question() -> None:
    """APPLICATION → DEEP_QUESTION（契约 3.5/3.6/3.9 + openapi Difficulty + 迁移映射）。"""
    assert set(enums.Difficulty) == {"BASIC", "UNDERSTANDING", "DEEP_QUESTION"}
    assert set(enums.Difficulty) == _openapi_enum("Difficulty")
    assert "APPLICATION" not in set(enums.Difficulty)
    assert task.DIFFICULTY_V25_MIGRATION == {"APPLICATION": "DEEP_QUESTION"}


def test_internal_stage_and_failure_stage_match_openapi() -> None:
    assert set(enums.TaskInternalStage) == _openapi_enum("TaskInternalStage")
    assert set(enums.FailureStage) == _openapi_enum("FailureStage")
    assert set(enums.TaskInternalStage) == {"PLANNING", "GENERATING", "SCORING", "PUBLISHING"}


def test_coverage_mode_matches_openapi() -> None:
    assert set(enums.CoverageMode) == {"COMPACT", "BALANCED", "EXTENSIVE"}
    assert set(enums.CoverageMode) == _openapi_enum("CoverageMode")


def test_publication_state_staged_published() -> None:
    """契约 3.9：publication_state = STAGED / PUBLISHED；历史卡均迁为 PUBLISHED。"""
    assert set(enums.PublicationState) == {"STAGED", "PUBLISHED"}
    assert set(enums.PublicationState) == _openapi_enum("Card", "publication_state")
    assert card.LEGACY_CARD_PUBLICATION_STATE == "PUBLISHED"


def test_visible_predicate_matches_contract_3_9() -> None:
    """统一可见谓词（契约 3.9）：publication_state='PUBLISHED' AND delete_batch_id IS NULL。"""
    assert card.VISIBLE_PREDICATE_SQL == (
        "publication_state = 'PUBLISHED' AND delete_batch_id IS NULL"
    )
    assert card.VISIBLE_PREDICATE_SQL_PARAMS == {
        "publication_state": "PUBLISHED",
        "delete_batch_id": None,
    }


def test_v25_new_resource_enums_match_openapi() -> None:
    assert set(enums.ProjectStatus) == {
        "EMPTY",
        "PARSING",
        "PARSE_FAILED",
        "AWAITING_CHAPTER_CONFIRMATION",
        "READY",
    }
    assert set(enums.ProjectStatus) == _openapi_enum("ProjectStatus")
    assert set(enums.DeletionBatchStatus) == {"PENDING", "UNDONE", "FINALIZED"}
    assert set(enums.DeletionBatchStatus) == _openapi_enum("DeletionBatchStatus")
    assert set(enums.RewritePreviewStatus) == {"PENDING", "APPLIED", "CANCELLED", "EXPIRED"}
    assert set(enums.RewritePreviewStatus) == _openapi_enum("RewritePreviewStatus")


def test_avatar_key_presets_match_openapi() -> None:
    assert (
        set(enums.AvatarKey)
        == {f"mood_{i:02d}" for i in range(1, 13)}
        == _openapi_enum("AuthUser", "avatar_key")
    )
    assert enums.AvatarKey.MOOD_01.value == "mood_01"


def test_preferences_defaults_and_ratio_semantics() -> None:
    """UserPreferences 默认值（契约 3.15）：BALANCED / 40/40/20 / 每日目标 50。"""
    assert preferences.DEFAULT_COVERAGE_MODE == "BALANCED"
    assert preferences.DEFAULT_DIFFICULTY_RATIO == {
        "basic": 40,
        "understanding": 40,
        "deep_question": 20,
    }
    assert preferences.DEFAULT_DAILY_LEARNING_GOAL == 50
    assert preferences.DIFFICULTY_RATIO_TOTAL == 100


def test_deletion_batch_and_rewrite_constants() -> None:
    """实现常量（契约 3.18/3.19）：撤销窗口 10 秒；重写预览 24 小时过期。"""
    assert deletion_batch.UNDO_WINDOW_SECONDS == 10
    assert rewrite_preview.REWRITE_PREVIEW_EXPIRY_HOURS == 24
