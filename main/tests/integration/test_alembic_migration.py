"""Alembic 迁移集成测试：空库 upgrade → 全表 + 约束；downgrade → 空库；再 upgrade 恢复。

V2.3 起 downgrade 显式拒绝（设备数据已物理删除，不可逆）——往返/清空测试的下界为
e85c78b2a345（V2.3 前终态），不再降过 V2.3。
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from infra.db.session import create_db_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "main" / "alembic.ini"


@pytest.fixture
def alembic_env(tmp_path: Path) -> tuple[Config, Path]:
    """返回 (config, db_path)：config 指向真实 alembic.ini 且 sqlalchemy.url 指向临时库。"""
    db_path = tmp_path / "migrated.db"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config, db_path


def _table_names(db_path: Path) -> set[str]:
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        return {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }


def test_alembic_upgrade_creates_all_tables(alembic_env: tuple[Config, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    tables = _table_names(db_path)
    expected = {
        "api_keys",
        "pdf_files",
        "chapters",
        "tasks",
        "knowledge_points",
        "batches",
        "decks",
        "cards",
        "review_states",
        "review_events",
        "idempotency_keys",
        "text_chunks",
        "llm_call_attempts",
        "users",
        "auth_sessions",
        "alembic_version",
    }
    assert expected <= tables


def test_alembic_downgrade_empties_db(alembic_env: tuple[Config, Path]) -> None:
    """downgrade → 空库（起点 e85c78b2a345：V2.3 downgrade 显式拒绝，不再降过）。"""
    config, db_path = alembic_env
    command.upgrade(config, "e85c78b2a345")
    command.downgrade(config, "base")
    # Alembic 不删除自身版本表：downgrade 到 base 后仅剩 alembic_version
    assert _table_names(db_path) == {"alembic_version"}


def test_alembic_upgrade_downgrade_upgrade_roundtrip(alembic_env: tuple[Config, Path]) -> None:
    """往返（下界 e85c78b2a345）：V2.3 起 downgrade 显式拒绝，不再降过 V2.3。"""
    config, db_path = alembic_env
    command.upgrade(config, "e85c78b2a345")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    tables = _table_names(db_path)
    assert "cards" in tables and "review_states" in tables
    assert "devices" not in tables  # V2.3 删除 devices 表


def test_alembic_0002_adds_request_body_hash(alembic_env: tuple[Config, Path]) -> None:
    """0002 增量迁移：idempotency_keys 增加 request_body_hash 列（database-design 2.12）。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info('idempotency_keys')"))}
    assert "request_body_hash" in cols


def test_alembic_users_auth_sessions_columns(alembic_env: tuple[Config, Path]) -> None:
    """P3-T1：users/auth_sessions 列集合与约束（username/token_hash UNIQUE、user_id FK）。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        users = {r[1]: r for r in conn.execute(text("PRAGMA table_info('users')"))}
        sessions = {r[1]: r for r in conn.execute(text("PRAGMA table_info('auth_sessions')"))}
        # SQLite 将 UNIQUE 约束实现为 sqlite_autoindex（不保留声明名），约束名在 sqlite_master SQL 文本中
        users_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        ).scalar_one()
        sessions_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='auth_sessions'")
        ).scalar_one()
        session_fks = conn.execute(text("PRAGMA foreign_key_list('auth_sessions')")).fetchall()
    assert set(users) == {"user_id", "username", "password_hash", "created_at", "updated_at"}
    assert users["user_id"][5] == 1  # PK
    assert users["username"][3] == 1  # NOT NULL
    assert users["password_hash"][3] == 1
    assert users["created_at"][3] == 1 and users["updated_at"][3] == 1
    assert "uq_users_username" in users_sql  # users.username UNIQUE
    assert set(sessions) == {
        "session_id",
        "user_id",
        "token_hash",
        "created_at",
        "expires_at",
        "revoked_at",
    }
    assert sessions["session_id"][5] == 1
    assert sessions["user_id"][3] == 1
    assert sessions["token_hash"][3] == 1
    assert sessions["created_at"][3] == 1 and sessions["expires_at"][3] == 1
    assert sessions["revoked_at"][3] == 0  # NULL
    assert "uq_auth_sessions_token_hash" in sessions_sql  # auth_sessions.token_hash UNIQUE
    # user_id FK → users（PRAGMA foreign_key_list 行：(id, seq, table, from, ...)）
    assert {(r[2], r[3]) for r in session_fks} == {("users", "user_id")}


def test_alembic_owner_tables_have_user_id(alembic_env: tuple[Config, Path]) -> None:
    """P3-T1：直接归属 6 表 user_id 列 + FK；V2.3 后 device_id 列与双非空 CHECK 已删除。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    owner_tables = ("pdf_files", "tasks", "decks", "cards", "review_events", "llm_call_attempts")
    with engine.connect() as conn:
        for table in owner_tables:
            cols = {r[1]: r for r in conn.execute(text(f"PRAGMA table_info('{table}')"))}
            sql_text = conn.execute(
                text(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
            ).scalar_one()
            fks = conn.execute(text(f"PRAGMA foreign_key_list('{table}')")).fetchall()
            assert "user_id" in cols, f"{table} 缺 user_id 列"
            assert "device_id" not in cols, f"{table}.device_id 应在 V2.3 删除"
            assert cols["user_id"][3] == 0, f"{table}.user_id 应为可空（非空由应用层保证）"
            assert f"ck_{table}_owner_domain" not in sql_text, (
                f"{table} 双非空 CHECK 应随 V2.3 删除"
            )
            assert any(r[3] == "user_id" and r[2] == "users" for r in fks), (
                f"{table}.user_id 缺 FK → users"
            )


def test_alembic_foreign_keys_and_checks_active(alembic_env: tuple[Config, Path]) -> None:
    """磁盘 SQLite：外键与 CHECK 约束真实生效（database-design 0/3）。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        # 部分唯一索引存在（PRAGMA index_list 第 1 列为 seq，第 2 列为索引名）
        indexes = {r[1] for r in conn.execute(text("PRAGMA index_list('cards')"))}
        # SQLite 把 UNIQUE 约束实现为 sqlite_autoindex_* 系统索引（不保留声明名）：
        # 用 origin='u' 的行确认 uq_cards_deck_position 唯一约束存在，且覆盖 (deck_id, position)
        unique_constraint_columns: set[str] = set()
        for r in conn.execute(text("PRAGMA index_list('cards')")):
            if r[3] == "u":  # origin: 'c'=CREATE INDEX / 'u'=UNIQUE 约束 / 'pk'=主键
                cols = {x[2] for x in conn.execute(text(f"PRAGMA index_info('{r[1]}')"))}
                unique_constraint_columns |= cols
    assert "ix_cards_gen_item_partial" in indexes
    assert unique_constraint_columns == {"deck_id", "position"}


def _unique_constraint_column_sets(conn: Connection, table: str) -> list[set[str]]:
    """PRAGMA index_list origin='u' 行的列集列表（SQLite 把 UNIQUE 约束实现为 sqlite_autoindex）。"""
    sets: list[set[str]] = []
    for r in conn.execute(text(f"PRAGMA index_list('{table}')")):
        if r[3] == "u":
            sets.append({x[2] for x in conn.execute(text(f"PRAGMA index_info('{r[1]}')"))})
    return sets


def _pk_column_order(conn: Connection, table: str) -> list[str]:
    """PRAGMA table_info 按主键序号（第 6 列，1-based）升序取列名 = 复合主键列序。"""
    rows = [r for r in conn.execute(text(f"PRAGMA table_info('{table}')")) if r[5]]
    return [r[1] for r in sorted(rows, key=lambda r: r[5])]


def test_alembic_api_keys_pk_rebuilt_to_user_id(alembic_env: tuple[Config, Path]) -> None:
    """P3-T2：api_keys 主键为 user_id（NULL PK）；V2.3 后 device_id 列/FK/CHECK 已删除。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cols = {r[1]: r for r in conn.execute(text("PRAGMA table_info('api_keys')"))}
        sql_text = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='api_keys'")
        ).scalar_one()
        fks = {(r[2], r[3]) for r in conn.execute(text("PRAGMA foreign_key_list('api_keys')"))}
    assert "user_id" in cols
    assert cols["user_id"][5] == 1, "api_keys.user_id 应为主键"
    assert cols["user_id"][3] == 0, "api_keys.user_id 主键可空（SQLite 非 INTEGER 主键允许 NULL）"
    assert "device_id" not in cols, "device_id 应在 V2.3 删除"
    assert "ck_api_keys_owner_domain" not in sql_text, "双非空 CHECK 应随 V2.3 删除"
    assert ("users", "user_id") in fks, "user_id 缺 FK → users"
    assert ("devices", "device_id") not in fks, "device_id FK → devices 应随 V2.3 删除"


def test_alembic_idempotency_pk_rebuilt_and_legacy_unique_kept(
    alembic_env: tuple[Config, Path],
) -> None:
    """P3-T2：idempotency_keys 主键 (user_id, path, idempotency_key)；V2.3 后遗留 device UNIQUE/列/CHECK 已删。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cols = {r[1]: r for r in conn.execute(text("PRAGMA table_info('idempotency_keys')"))}
        pk_order = _pk_column_order(conn, "idempotency_keys")
        unique_colsets = _unique_constraint_column_sets(conn, "idempotency_keys")
        sql_text = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='idempotency_keys'")
        ).scalar_one()
    assert pk_order == ["user_id", "path", "idempotency_key"]
    assert cols["user_id"][3] == 0, "user_id 主键可空（SQLite 非 INTEGER 主键允许 NULL）"
    assert "device_id" not in cols, "device_id 应在 V2.3 删除"
    assert {"device_id", "path", "idempotency_key"} not in unique_colsets, (
        "遗留唯一约束应随 V2.3 删除"
    )
    assert "ck_idempotency_keys_owner_domain" not in sql_text, "双非空 CHECK 应随 V2.3 删除"


def test_review_events_user_client_unique_added_legacy_kept(
    alembic_env: tuple[Config, Path],
) -> None:
    """P3-T2：review_events UNIQUE (user_id, client_event_id)；V2.3 后原 device 版 UNIQUE 已删。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        unique_colsets = _unique_constraint_column_sets(conn, "review_events")
    assert {"device_id", "client_event_id"} not in unique_colsets, "原 UNIQUE 应随 V2.3 删除"
    assert {"user_id", "client_event_id"} in unique_colsets, "另加 UNIQUE 应存在"


def test_alembic_api_keys_device_unique_removed_on_v2_3(
    alembic_env: tuple[Config, Path],
) -> None:
    """P4-5 跟进（V2.3 清除）：api_keys 的 UNIQUE (device_id) 与 device_id 列一并删除。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info('api_keys')"))}
        assert "device_id" not in cols
        assert {"device_id"} not in _unique_constraint_column_sets(conn, "api_keys")


# 旧库（2a391e994f93，v2.1 终态）device 域数据行：每表 1 行，SQL 直插（不经验 ORM）。
_LEGACY_INSERT_SQLS: tuple[str, ...] = (
    (
        "INSERT INTO devices (device_id, first_seen_ip, user_agent, last_active_at, created_at)"
        " VALUES ('dev1', NULL, NULL, NULL, '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO api_keys (device_id, encrypted_key, status, masked_key, updated_at)"
        " VALUES ('dev1', 'enc-1', 'AVAILABLE', 'sk-****abcd', '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO idempotency_keys (device_id, path, idempotency_key, response_status,"
        " response_body, request_body_hash, created_at)"
        " VALUES ('dev1', '/v1/pdfs/f1', 'ikey-1', 200, '{}', 'hash-1',"
        " '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO pdf_files (file_id, device_id, filename, storage_key, size_bytes, status,"
        " error_code, created_at)"
        " VALUES ('f1', 'dev1', 'a.pdf', 'stor-1', 10, 'PARSED', NULL,"
        " '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO tasks (task_id, device_id, file_id, deck_id, status, stage,"
        " selected_chapters, generation_config, cursor, generated_card_count,"
        " total_batch_count, completed_batch_count, resumable, failure_stage, error_code,"
        " created_at, started_at, ended_at, updated_at)"
        " VALUES ('t1', 'dev1', 'f1', NULL, 'COMPLETED', NULL, '[]', '{}', NULL, 0, NULL,"
        " NULL, 0, NULL, NULL, '2026-01-01T00:00:00.000Z', NULL, NULL, NULL)"
    ),
    (
        "INSERT INTO decks (deck_id, device_id, name, source, version, created_at, updated_at)"
        " VALUES ('d1', 'dev1', 'Deck 1', 'MANUAL', 'v1',"
        " '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO cards (card_id, deck_id, device_id, source, position, front, back, code,"
        " card_type, question, answer, statement, explanation, answer_boolean,"
        " generation_item_id, target_difficulty, knowledge_point_ids, evidence_score,"
        " correctness_score, difficulty_score, learning_value_score, rubric_total_score,"
        " version, created_at, updated_at)"
        " VALUES ('c1', 'd1', 'dev1', 'GENERATED', 1, 'front', 'back', NULL, 'QUESTION',"
        " 'q', 'a', NULL, NULL, NULL, 'g1', NULL, NULL, NULL, NULL, NULL, NULL, NULL,"
        " 'v1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO review_events (review_event_id, device_id, card_id, client_event_id,"
        " rating, reviewed_at, device_timezone, created_at)"
        " VALUES ('re1', 'dev1', 'c1', 'ce1', 'GOOD', '2026-01-01T00:00:00.000Z',"
        " 'Asia/Shanghai', '2026-01-01T00:00:00.000Z')"
    ),
    (
        "INSERT INTO llm_call_attempts (call_id, device_id, scope_type, scope_id, task_id,"
        " stage, operation_key, attempt_no, input_fingerprint, model, prompt_name,"
        " prompt_version, schema_name, schema_version, rubric_version, cache_hit, cache_miss,"
        " output_tokens, http_status, duration_ms, status, error_code, normalized_result,"
        " created_at, finished_at)"
        " VALUES ('ca1', 'dev1', 'TASK', 't1', 't1', 'PLANNING', 'op1', 1, 'fp1', 'm1',"
        " 'p1', 'v1', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'SUCCESS', NULL,"
        " NULL, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
    ),
)

# 直接归属 8 表（P3-T2 后均含 user_id；旧行 user_id 为 NULL）
_OWNER_TABLES: tuple[str, ...] = (
    "api_keys",
    "idempotency_keys",
    "pdf_files",
    "tasks",
    "decks",
    "cards",
    "review_events",
    "llm_call_attempts",
)


def _upgrade_legacy_db_with_rows(config: Config, db_path: Path) -> None:
    """在 2a391e994f93 旧库副本直插 device 域行后 upgrade 到 head。"""
    command.upgrade(config, "2a391e994f93")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for sql in _LEGACY_INSERT_SQLS:
            conn.execute(text(sql))
    command.upgrade(config, "head")


def test_legacy_device_rows_removed_on_v2_3(alembic_env: tuple[Config, Path]) -> None:
    """旧 device 域行随 V2.3 物理删除：user_id IS NULL 行清零、device_id 列不存在。"""
    config, db_path = alembic_env
    _upgrade_legacy_db_with_rows(config, db_path)  # 2a391e994f93 → 直插旧行 → upgrade head
    engine = create_db_engine(f"sqlite:///{db_path}")
    # 表名检查在同一连接内完成（不再新开引擎）：多引擎连串（迁移/直插/断言）交错时
    # 新引擎 BEGIN IMMEDIATE 可能撞上前一连接的 WAL checkpoint/滞留写锁
    # （database is locked，实测），单连接串行断言不受影响。
    with engine.begin() as conn:
        for table in _OWNER_TABLES:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")
            ).scalar()
            assert count == 0, f"{table} 旧 device 域行未删除"
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
            assert "device_id" not in cols
        tables = {
            r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "devices" not in tables


def test_v2_3_downgrade_rejected(alembic_env: tuple[Config, Path]) -> None:
    """V2.3 起 downgrade 显式拒绝：设备数据已物理删除，不可逆。"""
    config, _ = alembic_env
    command.upgrade(config, "head")
    with pytest.raises(RuntimeError, match="迁移不可逆"):
        command.downgrade(config, "e85c78b2a345")


def test_alembic_downgrade_fail_closed_with_user_data(alembic_env: tuple[Config, Path]) -> None:
    """P3-T2：存在用户域数据时 downgrade 在任何 DDL/DML 前拒绝（fail closed），表结构未变。

    V2.3 起 downgrade 显式拒绝（不可逆），fail-closed 守卫仅在 V2.3 之前的层可达：
    从 e85c78b2a345（V2.3 前终态）降 base 触发。
    """
    config, db_path = alembic_env
    command.upgrade(config, "e85c78b2a345")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (user_id, username, password_hash, created_at, updated_at)"
                " VALUES ('u1', 'alice', 'hash', '2026-01-01T00:00:00.000Z',"
                " '2026-01-01T00:00:00.000Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO decks (deck_id, device_id, user_id, name, source, version,"
                " created_at, updated_at)"
                " VALUES ('d1', NULL, 'u1', 'Deck 1', 'MANUAL', 'v1',"
                " '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')"
            )
        )
    before = _table_names(db_path)
    with pytest.raises(RuntimeError, match="fail closed"):
        command.downgrade(config, "base")
    assert _table_names(db_path) == before, "downgrade 拒绝后表结构不得变化"


def test_alembic_empty_and_legacy_only_downgrade_ok(alembic_env: tuple[Config, Path]) -> None:
    """P3-T2：空库往返下界 e85c78b2a345（V2.3 downgrade 显式拒绝、不再降过 V2.3）。"""
    config, _ = alembic_env
    # 空库 upgrade → downgrade → upgrade 往返（下界 e85c78b2a345）
    command.upgrade(config, "e85c78b2a345")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # 从 head 降过 V2.3 显式拒绝（downgrade 目标 e85c78b2a345）
    with pytest.raises(RuntimeError, match="迁移不可逆"):
        command.downgrade(config, "e85c78b2a345")
