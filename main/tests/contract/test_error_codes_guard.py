"""契约守卫 3：错误码清单 ↔ structure-contract 第 7 章（project-structure 5，红线 1）。"""

from app.errors import ERROR_HTTP_STATUS, ErrorCode
from tests.contract.support import STRUCTURE_CONTRACT_PATH, parse_error_codes_table


def test_error_codes_match_contract_chapter7() -> None:
    doc_codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    code_registry = {code.value: ERROR_HTTP_STATUS[code] for code in ErrorCode}
    assert code_registry == doc_codes


def test_rewrite_schema_invalid_registered() -> None:
    """V6：重写 Schema 校验失败专用码 422（ch7 表 + errors.py 全等由守卫校验）。"""
    from app.errors import ERROR_HTTP_STATUS, ErrorCode

    assert ErrorCode.REWRITE_SCHEMA_INVALID.value == "REWRITE_SCHEMA_INVALID"
    assert ERROR_HTTP_STATUS[ErrorCode.REWRITE_SCHEMA_INVALID] == 422
    codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert codes["REWRITE_SCHEMA_INVALID"] == 422


def test_v25_error_codes_registered() -> None:
    """V2.5 新错误码（第 7 章 偏好/PDF项目/任务/牌组卡片 分组）。"""
    expected: dict[str, int] = {
        "INVALID_PREFERENCES": 400,
        "INVALID_LEARNING_TIMEZONE": 400,
        "PROJECT_NOT_FOUND": 404,
        "PROJECT_STATE_CONFLICT": 409,
        "PROJECT_HAS_ACTIVE_TASK": 409,
        "TASK_ZERO_CARDS": 422,
        "SAMPLE_STALE": 409,
        "CARD_DELETE_WINDOW_EXPIRED": 409,
        "CARD_REWRITE_UNAVAILABLE": 409,
        "CARD_VERSION_CONFLICT": 409,
    }
    for code, status in expected.items():
        assert ErrorCode[code].value == code, f"缺少 V2.5 错误码 {code}"
        assert ERROR_HTTP_STATUS[ErrorCode[code]] == status, f"{code} HTTP 状态不符"
    # V2.5 删除用户侧暂停语义：TASK_NOT_RESUMABLE 不得再注册（resume 移出契约）
    assert not hasattr(ErrorCode, "TASK_NOT_RESUMABLE")
