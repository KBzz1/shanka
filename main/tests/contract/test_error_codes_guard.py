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
