"""契约守卫：PdfFile/Chapter ↔ openapi（守卫 1 扩展）。

openapi PdfFile 的 `chapters: Chapter[] | null`（type: [array, 'null'] + items $ref Chapter）
走守卫 array-of-object 嵌套路径；status 是 `$ref PdfStatus`（string enum）——str 注解
不校验 enum 值集（既有口径），值集一致性由 structure-contract 状态机契约承载。
"""

from app.schemas.pdfs import Chapter, PdfFile
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_pdf_file_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(PdfFile, openapi_schema("PdfFile"), load_openapi())
    assert violations == []


def test_chapter_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(Chapter, openapi_schema("Chapter"), load_openapi())
    assert violations == []
