"""契约守卫 2：infra/db ORM ↔ database-design.md（project-structure 5，红线 2）。

校验：表名集合全等；每表列名集合全等（ORM 模型字段 ↔ database-design 列）；
主键列集合全等（含 2.12 复合主键列序）；关键类型映射抽查（时间列 TEXT、布尔 INTEGER、小数 REAL）。
"""

import re

from infra.db.models import Base
from tests.contract.support import DATABASE_DESIGN_PATH, parse_database_tables

DOC_TABLES = parse_database_tables(DATABASE_DESIGN_PATH.read_text(encoding="utf-8"))
ORM_TABLES = {name: table for name, table in Base.metadata.tables.items()}


def test_orm_table_names_match_database_design() -> None:
    orm_names = set(ORM_TABLES)
    doc_names = set(DOC_TABLES)
    assert orm_names == doc_names


def test_orm_columns_match_database_design() -> None:
    for name, doc_cols in DOC_TABLES.items():
        orm_cols = set(ORM_TABLES[name].columns.keys())
        assert orm_cols == set(doc_cols), (
            f"{name}: ORM 列 {orm_cols} != database-design {set(doc_cols)}"
        )


def test_orm_primary_keys_match_database_design() -> None:
    for name, table in ORM_TABLES.items():
        pk = {c.name for c in table.primary_key.columns}
        doc_cols = DOC_TABLES[name]
        doc_pk = {col for col, decl in doc_cols.items() if "PK" in decl or "复合主键" in decl}
        assert pk == doc_pk, f"{name}: ORM PK {pk} != database-design {doc_pk}"


def test_orm_type_mapping_follows_convention() -> None:
    """database-design §0 类型映射抽查：时间/枚举→TEXT、布尔→INTEGER、小数→REAL、JSON→TEXT。"""
    time_cols = {
        "created_at",
        "updated_at",
        "last_active_at",
        "started_at",
        "ended_at",
        "due",
        "last_review",
        "reviewed_at",
    }
    for name, table in ORM_TABLES.items():
        for col in table.columns.values():
            if col.name in time_cols:
                assert str(col.type) in ("VARCHAR", "TEXT"), (
                    f"{name}.{col.name}: 时间列应为 TEXT，实际 {col.type}"
                )
    # 布尔/枚举抽查
    assert str(ORM_TABLES["cards"].c.answer_boolean.type).startswith("INTEGER")
    assert str(ORM_TABLES["review_states"].c.stability.type).startswith("REAL")
    assert str(ORM_TABLES["cards"].c.card_type.type) in ("VARCHAR", "TEXT")


def test_orm_idempotency_pk_order_matches_design() -> None:
    """database-design 2.12 主键注释 `PRIMARY KEY (device_id, path, idempotency_key)` 为权威列序。"""
    text = DATABASE_DESIGN_PATH.read_text(encoding="utf-8")
    m = re.search(r"主键:`PRIMARY KEY \(([a-z_, ]+)\)`", text)
    assert m is not None, "database-design 2.12 缺少主键注释行"
    design_order = [c.strip() for c in m.group(1).split(",")]
    orm_order = [c.name for c in ORM_TABLES["idempotency_keys"].primary_key.columns]
    assert orm_order == design_order
