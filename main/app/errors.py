"""统一错误对象与错误码注册表（structure-contract 1.4 / 7；红线 3：格式统一于 app/middleware）。

R-01 解决（唯一位置与派生规则）：
- 错误码唯一注册位置 = 本模块 `ErrorCode`；
- `localization_key` 由错误码派生：`error.` + 错误码 snake_case（如 DECK_NOT_FOUND → error.deck_not_found）；
- 文案清单唯一位置 = `LOCALIZATION_KEYS` 显式集合；契约守卫校验派生集合与清单全等，不另建文件。
错误码 ↔ HTTP 状态 ↔ structure-contract 第 7 章的一致性由 tests/contract 守卫校验。
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    # 通用
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # 账号（V2.2，决策 D-05；401 一律携带 WWW-Authenticate: Bearer）
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    # 偏好（V2.5）
    INVALID_PREFERENCES = "INVALID_PREFERENCES"
    INVALID_LEARNING_TIMEZONE = "INVALID_LEARNING_TIMEZONE"
    # PDF/项目
    PDF_UPLOAD_INVALID = "PDF_UPLOAD_INVALID"
    PDF_PARSE_FAILED = "PDF_PARSE_FAILED"
    PDF_TOC_MISSING = "PDF_TOC_MISSING"
    PDF_NOT_FOUND = "PDF_NOT_FOUND"
    CHAPTER_NOT_FOUND = "CHAPTER_NOT_FOUND"
    PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"  # V2.5 项目不存在或跨用户（统一 404）
    PROJECT_STATE_CONFLICT = "PROJECT_STATE_CONFLICT"  # V2.5 当前项目状态不允许操作
    PROJECT_HAS_ACTIVE_TASK = "PROJECT_HAS_ACTIVE_TASK"  # V2.5 删除被活跃任务阻止
    # API Key
    API_KEY_UNAVAILABLE = "API_KEY_UNAVAILABLE"
    API_KEY_NOT_SET = "API_KEY_NOT_SET"
    # 任务
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_STATE_CONFLICT = "TASK_STATE_CONFLICT"
    TASK_ZERO_CARDS = "TASK_ZERO_CARDS"  # V2.5 正式生成无有效卡（整体失败）
    SAMPLE_STALE = "SAMPLE_STALE"  # V2.5 配置变化后仍尝试确认旧样卡
    TASK_IN_PROGRESS = "TASK_IN_PROGRESS"
    GENERATION_FAILED = "GENERATION_FAILED"
    # 牌组/卡片
    DECK_NOT_FOUND = "DECK_NOT_FOUND"
    CARD_NOT_FOUND = "CARD_NOT_FOUND"
    GENERATION_ITEM_CONFLICT = "GENERATION_ITEM_CONFLICT"
    IMPORT_PARSE_ERROR = "IMPORT_PARSE_ERROR"
    CARD_DELETE_WINDOW_EXPIRED = "CARD_DELETE_WINDOW_EXPIRED"  # V2.5 撤销窗口已过
    CARD_REWRITE_UNAVAILABLE = "CARD_REWRITE_UNAVAILABLE"  # V2.5 来源已失效或非生成卡
    CARD_VERSION_CONFLICT = "CARD_VERSION_CONFLICT"  # V2.5 重写预览基于旧版本（CAS 失败）
    REWRITE_SCHEMA_INVALID = "REWRITE_SCHEMA_INVALID"
    # 复习
    REVIEW_EVENT_INVALID = "REVIEW_EVENT_INVALID"
    REVIEW_EVENT_CONFLICT = "REVIEW_EVENT_CONFLICT"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.AUTH_INVALID: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.EMAIL_TAKEN: 409,
    ErrorCode.INVALID_PREFERENCES: 400,
    ErrorCode.INVALID_LEARNING_TIMEZONE: 400,
    ErrorCode.PDF_UPLOAD_INVALID: 400,
    ErrorCode.PDF_PARSE_FAILED: 422,
    ErrorCode.PDF_TOC_MISSING: 422,
    ErrorCode.PDF_NOT_FOUND: 404,
    ErrorCode.CHAPTER_NOT_FOUND: 404,
    ErrorCode.PROJECT_NOT_FOUND: 404,
    ErrorCode.PROJECT_STATE_CONFLICT: 409,
    ErrorCode.PROJECT_HAS_ACTIVE_TASK: 409,
    ErrorCode.API_KEY_UNAVAILABLE: 502,
    ErrorCode.API_KEY_NOT_SET: 422,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.TASK_STATE_CONFLICT: 409,
    ErrorCode.TASK_ZERO_CARDS: 422,
    ErrorCode.SAMPLE_STALE: 409,
    ErrorCode.TASK_IN_PROGRESS: 409,
    ErrorCode.GENERATION_FAILED: 500,
    ErrorCode.DECK_NOT_FOUND: 404,
    ErrorCode.CARD_NOT_FOUND: 404,
    ErrorCode.GENERATION_ITEM_CONFLICT: 409,
    ErrorCode.IMPORT_PARSE_ERROR: 422,
    ErrorCode.CARD_DELETE_WINDOW_EXPIRED: 409,
    ErrorCode.CARD_REWRITE_UNAVAILABLE: 409,
    ErrorCode.CARD_VERSION_CONFLICT: 409,
    ErrorCode.REWRITE_SCHEMA_INVALID: 422,
    ErrorCode.REVIEW_EVENT_INVALID: 400,
    ErrorCode.REVIEW_EVENT_CONFLICT: 409,
}

# 文案清单（唯一位置，R-01）：派生集合的显式快照，守卫校验与派生集合全等
LOCALIZATION_KEYS: frozenset[str] = frozenset(
    {
        "error.validation_error",
        "error.rate_limited",
        "error.idempotency_conflict",
        "error.internal_error",
        "error.auth_required",
        "error.auth_invalid",
        "error.invalid_credentials",
        "error.email_taken",
        "error.invalid_preferences",
        "error.invalid_learning_timezone",
        "error.pdf_upload_invalid",
        "error.pdf_parse_failed",
        "error.pdf_toc_missing",
        "error.pdf_not_found",
        "error.chapter_not_found",
        "error.project_not_found",
        "error.project_state_conflict",
        "error.project_has_active_task",
        "error.api_key_unavailable",
        "error.api_key_not_set",
        "error.task_not_found",
        "error.task_state_conflict",
        "error.task_zero_cards",
        "error.sample_stale",
        "error.task_in_progress",
        "error.generation_failed",
        "error.deck_not_found",
        "error.card_not_found",
        "error.generation_item_conflict",
        "error.import_parse_error",
        "error.card_delete_window_expired",
        "error.card_rewrite_unavailable",
        "error.card_version_conflict",
        "error.rewrite_schema_invalid",
        "error.review_event_invalid",
        "error.review_event_conflict",
    }
)


def http_status(code: ErrorCode) -> int:
    return ERROR_HTTP_STATUS[code]


def localization_key(code: ErrorCode) -> str:
    """错误码 → localization_key 派生规则（R-01）。"""
    return "error." + code.value.lower()


class AppError(Exception):
    """统一业务错误：handler 层映射为 1.4 错误响应；message 仅面向用户，内部细节只进日志。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_response(self) -> dict[str, dict[str, str]]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "localization_key": localization_key(self.code),
            }
        }
