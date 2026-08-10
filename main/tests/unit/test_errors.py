"""app.errors 统一错误对象单元测试（structure-contract 1.4 / 7）。"""

import re

from app.errors import (
    ERROR_HTTP_STATUS,
    LOCALIZATION_KEYS,
    AppError,
    ErrorCode,
    http_status,
    localization_key,
)


def test_errors_http_status_covers_all_codes() -> None:
    for code in ErrorCode:
        assert code in ERROR_HTTP_STATUS
        assert 100 <= ERROR_HTTP_STATUS[code] <= 599


def test_errors_http_status_values_match_contract() -> None:
    assert http_status(ErrorCode.DECK_NOT_FOUND) == 404
    assert http_status(ErrorCode.IDEMPOTENCY_CONFLICT) == 409
    assert http_status(ErrorCode.RATE_LIMITED) == 429
    assert http_status(ErrorCode.API_KEY_UNAVAILABLE) == 502


def test_errors_localization_key_derivation() -> None:
    assert localization_key(ErrorCode.DECK_NOT_FOUND) == "error.deck_not_found"
    assert localization_key(ErrorCode.PDF_PARSE_FAILED) == "error.pdf_parse_failed"


def test_errors_localization_keys_explicit_list_matches_derived() -> None:
    derived = frozenset(localization_key(code) for code in ErrorCode)
    assert derived == LOCALIZATION_KEYS


def test_errors_localization_key_format() -> None:
    for key in LOCALIZATION_KEYS:
        assert re.fullmatch(r"error\.[a-z0-9_]+", key)


def test_errors_app_error_response_shape() -> None:
    err = AppError(ErrorCode.DECK_NOT_FOUND, "未找到牌组")
    assert err.code is ErrorCode.DECK_NOT_FOUND
    assert err.message == "未找到牌组"
    assert err.to_response() == {
        "error": {
            "code": "DECK_NOT_FOUND",
            "message": "未找到牌组",
            "localization_key": "error.deck_not_found",
        }
    }
