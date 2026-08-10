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
    # 设备
    DEVICE_ID_REQUIRED = "DEVICE_ID_REQUIRED"
    DEVICE_ID_INVALID = "DEVICE_ID_INVALID"
    # PDF
    PDF_UPLOAD_INVALID = "PDF_UPLOAD_INVALID"
    PDF_PARSE_FAILED = "PDF_PARSE_FAILED"
    PDF_TOC_MISSING = "PDF_TOC_MISSING"
    PDF_NOT_FOUND = "PDF_NOT_FOUND"
    # API Key
    API_KEY_UNAVAILABLE = "API_KEY_UNAVAILABLE"
    API_KEY_NOT_SET = "API_KEY_NOT_SET"
    # 任务
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_STATE_CONFLICT = "TASK_STATE_CONFLICT"
    TASK_NOT_RESUMABLE = "TASK_NOT_RESUMABLE"
    TASK_IN_PROGRESS = "TASK_IN_PROGRESS"
    GENERATION_FAILED = "GENERATION_FAILED"
    # 牌组/卡片
    DECK_NOT_FOUND = "DECK_NOT_FOUND"
    CARD_NOT_FOUND = "CARD_NOT_FOUND"
    GENERATION_ITEM_CONFLICT = "GENERATION_ITEM_CONFLICT"
    IMPORT_PARSE_ERROR = "IMPORT_PARSE_ERROR"
    # 复习
    REVIEW_EVENT_INVALID = "REVIEW_EVENT_INVALID"
    REVIEW_EVENT_CONFLICT = "REVIEW_EVENT_CONFLICT"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.DEVICE_ID_REQUIRED: 401,
    ErrorCode.DEVICE_ID_INVALID: 401,
    ErrorCode.PDF_UPLOAD_INVALID: 400,
    ErrorCode.PDF_PARSE_FAILED: 422,
    ErrorCode.PDF_TOC_MISSING: 422,
    ErrorCode.PDF_NOT_FOUND: 404,
    ErrorCode.API_KEY_UNAVAILABLE: 502,
    ErrorCode.API_KEY_NOT_SET: 422,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.TASK_STATE_CONFLICT: 409,
    ErrorCode.TASK_NOT_RESUMABLE: 409,
    ErrorCode.TASK_IN_PROGRESS: 409,
    ErrorCode.GENERATION_FAILED: 500,
    ErrorCode.DECK_NOT_FOUND: 404,
    ErrorCode.CARD_NOT_FOUND: 404,
    ErrorCode.GENERATION_ITEM_CONFLICT: 409,
    ErrorCode.IMPORT_PARSE_ERROR: 422,
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
        "error.device_id_required",
        "error.device_id_invalid",
        "error.pdf_upload_invalid",
        "error.pdf_parse_failed",
        "error.pdf_toc_missing",
        "error.pdf_not_found",
        "error.api_key_unavailable",
        "error.api_key_not_set",
        "error.task_not_found",
        "error.task_state_conflict",
        "error.task_not_resumable",
        "error.task_in_progress",
        "error.generation_failed",
        "error.deck_not_found",
        "error.card_not_found",
        "error.generation_item_conflict",
        "error.import_parse_error",
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
