"""契约守卫 2：infra/db ORM ↔ database-design.md（project-structure 5，红线 2）。

校验：表名集合全等；每表列名集合全等（ORM 模型字段 ↔ database-design 列）；
主键列集合全等（含 2.12 复合主键列序）；关键类型映射抽查（时间列 TEXT、布尔 INTEGER、小数 REAL）。
"""

import re

from sqlalchemy import UniqueConstraint

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


# ---------- V2.5 守卫（database-design 2.17~2.21 / 7.3） ----------


def test_v25_new_tables_present() -> None:
    """V2.5 新表：learning_projects / materials（V25-D-29 多资料）/ user_preferences /
    project_study_settings / card_deletion_batches / card_rewrite_previews
    （database-design 2.17~2.22）。"""
    for table in (
        "learning_projects",
        "materials",
        "user_preferences",
        "project_study_settings",
        "card_deletion_batches",
        "card_rewrite_previews",
    ):
        assert table in ORM_TABLES


def test_v25_materials_columns_and_text_chunks_key() -> None:
    """materials 列（database-design 2.22；PDF 行 status NULL 以 pdf_files 为权威）；
    text_chunks 唯一键改 (material_id, chunk_seq)（V25-D-29/32）。"""
    materials = ORM_TABLES["materials"]
    for col in (
        "material_id",
        "project_id",
        "type",
        "name",
        "status",
        "error_code",
        "size_bytes",
        "char_count",
        "created_at",
    ):
        assert col in materials.columns, f"materials 缺列 {col}"
    assert materials.c.status.nullable, "materials.status 应可空（PDF 行 NULL）"
    chunks = ORM_TABLES["text_chunks"]
    uq = [c for c in chunks.constraints if isinstance(c, UniqueConstraint)]
    assert any({col.name for col in u.columns} == {"material_id", "chunk_seq"} for u in uq), (
        "text_chunks 唯一键应为 (material_id, chunk_seq)"
    )
    assert chunks.c.material_id.nullable is False
    assert chunks.c.chunk_seq.nullable is False
    assert chunks.c.file_id.nullable, "text_chunks.file_id 应可空（TEXT 资料无 PDF 行）"


def test_v25_new_table_fks() -> None:
    """新表外键（database-design 2.17~2.22）：
    materials.project_id FK CASCADE（V25-D-29 资料集合权威归属）；user_preferences.user_id
    PK FK → users；project_study_settings.project_id PK FK CASCADE；删除批次/重写预览
    user_id FK → users、重写预览 card_id FK → cards CASCADE。"""
    materials = ORM_TABLES["materials"]
    project_fk = [c for c in materials.c.project_id.foreign_keys]
    assert len(project_fk) == 1 and project_fk[0].column.table.name == "learning_projects"
    assert project_fk[0].ondelete == "CASCADE"
    assert "file_id" not in ORM_TABLES["learning_projects"].columns, (
        "V25-D-29 后 learning_projects 不再持有 file_id（资料归属经 materials 表）"
    )
    assert ORM_TABLES["user_preferences"].c.user_id.primary_key
    assert any(
        c.column.table.name == "users"
        for c in ORM_TABLES["user_preferences"].c.user_id.foreign_keys
    )
    pss_fk = [c for c in ORM_TABLES["project_study_settings"].c.project_id.foreign_keys]
    assert len(pss_fk) == 1 and pss_fk[0].column.table.name == "learning_projects"
    assert pss_fk[0].ondelete == "CASCADE"
    assert ORM_TABLES["card_rewrite_previews"].c.card_id.foreign_keys
    assert ORM_TABLES["card_deletion_batches"].c.user_id.foreign_keys


def test_v25_tasks_new_columns() -> None:
    """tasks 新增列（database-design 2.5）：project_id FK SET NULL、retry_of_task_id FK
    SET NULL（只指向同用户失败任务）、sample_cards/sample_config_hash/sample_confirmed_at。"""
    tasks = ORM_TABLES["tasks"]
    for col in (
        "project_id",
        "retry_of_task_id",
        "sample_cards",
        "sample_config_hash",
        "sample_confirmed_at",
    ):
        assert col in tasks.columns
    assert any(
        c.column.table.name == "learning_projects" and c.ondelete == "SET NULL"
        for c in tasks.c.project_id.foreign_keys
    )
    assert any(
        c.column.table.name == "tasks" and c.ondelete == "SET NULL"
        for c in tasks.c.retry_of_task_id.foreign_keys
    )


def test_v25_cards_publication_columns() -> None:
    """cards V2.5 列（database-design 2.9）：source_task_id FK SET NULL、chapter_id FK
    SET NULL、publication_state NOT NULL DEFAULT 'PUBLISHED'、delete_batch_id FK
    SET NULL、pending_delete_at/undo_until。"""
    cards = ORM_TABLES["cards"]
    for col in (
        "source_task_id",
        "chapter_id",
        "publication_state",
        "delete_batch_id",
        "pending_delete_at",
        "undo_until",
    ):
        assert col in cards.columns
    pub = cards.c.publication_state
    assert not pub.nullable
    assert pub.default is not None or pub.server_default is not None
    assert any(
        c.column.table.name == "card_deletion_batches" and c.ondelete == "SET NULL"
        for c in cards.c.delete_batch_id.foreign_keys
    )


def test_v25_visible_predicate_index() -> None:
    """统一可见谓词索引 (publication_state, delete_batch_id)（database-design 2.9 契约 3.9）。"""
    cards = ORM_TABLES["cards"]
    idx = [
        i
        for i in cards.indexes
        if set(i.columns.keys()) == {"publication_state", "delete_batch_id"}
    ]
    assert idx, "cards 缺 (publication_state, delete_batch_id) 索引"
    assert {i.name for i in cards.indexes} >= {
        "ix_cards_user_deck",
        "ix_cards_source_task",
        "ix_cards_chapter_id",
    }


def test_v25_decks_project_column() -> None:
    """decks.project_id（database-design 2.8）：NULL FK → learning_projects SET NULL。"""
    decks = ORM_TABLES["decks"]
    assert "project_id" in decks.columns
    assert any(
        c.column.table.name == "learning_projects" and c.ondelete == "SET NULL"
        for c in decks.c.project_id.foreign_keys
    )
    assert any(set(i.columns.keys()) == {"project_id"} for i in decks.indexes), (
        "decks 缺 (project_id) 索引"
    )


def test_v25_users_avatar_key() -> None:
    """users.avatar_key（database-design 2.15）：NOT NULL DEFAULT 'mood_01'。"""
    users = ORM_TABLES["users"]
    avatar = users.c.avatar_key
    assert not avatar.nullable
    assert avatar.default is not None or avatar.server_default is not None


def test_v25_user_preferences_ratio_checks() -> None:
    """user_preferences 比例约束（database-design 2.18）：三档 10% 整数档 0~100、合计 100；
    daily_goal 10~200 且 10 的倍数。"""
    prefs = ORM_TABLES["user_preferences"]
    assert prefs.c.basic_ratio.nullable is False
    assert "deep_question_ratio" in prefs.c
    from sqlalchemy import CheckConstraint

    check_sql = "\n".join(
        c.sqltext.text for c in prefs.constraints if isinstance(c, CheckConstraint)
    )
    assert "basic_ratio" in check_sql
    assert "basic_ratio + understanding_ratio + deep_question_ratio = 100" in check_sql
    assert "daily_goal" in check_sql
    # 默认值（契约 3.15）：BALANCED / 40/40/20 / 每日目标 50
    assert (
        prefs.c.coverage_mode.default is not None
        or prefs.c.coverage_mode.server_default is not None
    )


def test_v25_review_event_device_timezone_nullable() -> None:
    """review_events.device_timezone（database-design 2.11）：V2.5 降级为可空审计字段。"""
    col = ORM_TABLES["review_events"].c.device_timezone
    assert col.nullable is True
