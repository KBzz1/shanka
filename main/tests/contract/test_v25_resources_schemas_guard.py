"""契约守卫（V2.5 扩展）：V2.5 新增资源 ↔ openapi.yaml（守卫 1 扩展，红线 1）。

锚点：UserPreferences / UserPreferencesUpdateRequest / LearningProject /
ProjectStudySettings / ProjectStudySettingsUpdateRequest / CardDeletionBatch /
CardRewritePreview / TodayStudyPlan / TodayPlanCard / TaskUpdateRequest /
ReviewEvent / ReviewEventRequest / AuthUser / AuthMeUpdateRequest。
枚举字段沿用既有口径：str 注解不校验 enum 值集，值集一致性由 domain/enums 守卫
（test_domain_enums_guard）承载。
"""

from app.schemas.auth import AuthMeUpdateRequest, AuthUser
from app.schemas.deletion_batch import CardDeletionBatch
from app.schemas.preferences import (
    UserPreferences,
    UserPreferencesUpdateRequest,
)
from app.schemas.project import (
    LearningProject,
    ProjectStudySettings,
    ProjectStudySettingsUpdateRequest,
)
from app.schemas.review import ReviewEvent, ReviewEventRequest
from app.schemas.rewrite_preview import CardRewritePreview
from app.schemas.study_plan import TodayPlanCard, TodayStudyPlan
from app.schemas.tasks import TaskUpdateRequest
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def _consistent(model: type, schema_name: str) -> None:
    violations = check_schema_consistency(model, openapi_schema(schema_name), load_openapi())
    assert violations == []


def test_user_preferences_schema_openapi_consistent() -> None:
    _consistent(UserPreferences, "UserPreferences")


def test_user_preferences_update_request_schema_openapi_consistent() -> None:
    _consistent(UserPreferencesUpdateRequest, "UserPreferencesUpdateRequest")


def test_learning_project_schema_openapi_consistent() -> None:
    _consistent(LearningProject, "LearningProject")


def test_project_study_settings_schema_openapi_consistent() -> None:
    _consistent(ProjectStudySettings, "ProjectStudySettings")


def test_project_study_settings_update_request_schema_openapi_consistent() -> None:
    _consistent(ProjectStudySettingsUpdateRequest, "ProjectStudySettingsUpdateRequest")


def test_card_deletion_batch_schema_openapi_consistent() -> None:
    _consistent(CardDeletionBatch, "CardDeletionBatch")


def test_card_rewrite_preview_schema_openapi_consistent() -> None:
    _consistent(CardRewritePreview, "CardRewritePreview")


def test_today_study_plan_schema_openapi_consistent() -> None:
    _consistent(TodayStudyPlan, "TodayStudyPlan")


def test_today_plan_card_schema_openapi_consistent() -> None:
    """TodayPlanCard = allOf(Card + {review_state, forgetting_risk}) 平铺（Card 继承）。"""
    _consistent(TodayPlanCard, "TodayPlanCard")


def test_task_update_request_schema_openapi_consistent() -> None:
    _consistent(TaskUpdateRequest, "TaskUpdateRequest")


def test_review_event_schema_openapi_consistent() -> None:
    """ReviewEvent 为内部不可变记录（契约 3.11），视图模型作为守卫锚点。"""
    _consistent(ReviewEvent, "ReviewEvent")


def test_review_event_request_schema_openapi_consistent() -> None:
    """V2.5：请求不再要求 device_timezone（可空审计字段）。"""
    _consistent(ReviewEventRequest, "ReviewEventRequest")


def test_auth_user_schema_openapi_consistent() -> None:
    """V2.5：AuthUser 含 email（只读）与 avatar_key（预设头像）。"""
    _consistent(AuthUser, "AuthUser")


def test_auth_me_update_request_schema_openapi_consistent() -> None:
    _consistent(AuthMeUpdateRequest, "AuthMeUpdateRequest")
