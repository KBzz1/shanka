"""契约守卫 1：app/schemas ↔ openapi.yaml（project-structure 5，红线 1）。"""

from pydantic import BaseModel

from app.schemas.common import ErrorResponse
from tests.contract.support import (
    STRUCTURE_CONTRACT_PATH,
    check_schema_consistency,
    load_openapi,
    openapi_schema,
    parse_error_codes_table,
)


def test_schema_openapi_error_consistent() -> None:
    violations = check_schema_consistency(ErrorResponse, openapi_schema("Error"), load_openapi())
    assert violations == []


def test_schema_guard_detects_extra_model_field() -> None:
    """负例：守卫必须具备真牙口——模型多出字段必须被检出。"""

    class Drifted(BaseModel):
        code: str
        message: str
        localization_key: str
        extra_field: str

    nested = openapi_schema("Error")["properties"]["error"]
    violations = check_schema_consistency(Drifted, nested, load_openapi())
    assert any("extra_field" in v for v in violations)


def test_schema_guard_detects_missing_required_field() -> None:
    """负例：openapi 必填字段在模型中可选必须被检出。"""

    class MissingRequired(BaseModel):
        code: str
        message: str
        localization_key: str | None = None

    nested = openapi_schema("Error")["properties"]["error"]
    violations = check_schema_consistency(MissingRequired, nested, load_openapi())
    assert any("localization_key" in v for v in violations)


def test_error_codes_table_parses_with_group_prefix() -> None:
    """防假阴性回归：第 7 章表格带分组前缀列，错误码仍须全部解析出来（守卫 3 前置）。"""
    codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert codes, "错误码表解析结果为空（分组前缀列导致假阴性）"
    assert codes["DECK_NOT_FOUND"] == 404
    assert codes["REVIEW_EVENT_CONFLICT"] == 409
