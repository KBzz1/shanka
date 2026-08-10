"""契约守卫 3：错误码清单 ↔ structure-contract 第 7 章（project-structure 5，红线 1）。"""

from app.errors import ERROR_HTTP_STATUS, ErrorCode
from tests.contract.support import STRUCTURE_CONTRACT_PATH, parse_error_codes_table


def test_error_codes_match_contract_chapter7() -> None:
    doc_codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    code_registry = {code.value: ERROR_HTTP_STATUS[code] for code in ErrorCode}
    assert code_registry == doc_codes
