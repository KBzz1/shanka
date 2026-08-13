"""Alembic 迁移集成测试：空库 upgrade → 12 表 + 约束；downgrade → 空库；再 upgrade 恢复。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

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
        "devices",
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
    config, db_path = alembic_env
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    # Alembic 不删除自身版本表：downgrade 到 base 后仅剩 alembic_version
    assert _table_names(db_path) == {"alembic_version"}


def test_alembic_upgrade_downgrade_upgrade_roundtrip(alembic_env: tuple[Config, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    tables = _table_names(db_path)
    assert "cards" in tables and "review_states" in tables


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
    """P3-T1：直接归属 6 表 user_id 列、device_id 降级可空、双非空 CHECK 存在。"""
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
            assert cols["device_id"][3] == 0, f"{table}.device_id 应为可空（遗留列）"
            assert cols["user_id"][3] == 0, f"{table}.user_id 应为可空（双非空由 CHECK 保证）"
            assert f"ck_{table}_owner_domain" in sql_text, f"{table} 缺双非空 CHECK"
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
    """P3-T2：api_keys 主键重建为 user_id（NULL PK），device_id 为普通 NULL 遗留列。"""
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
    assert cols["user_id"][3] == 0, (
        "api_keys.user_id 主键可空（旧行保留，SQLite 非 INTEGER 主键允许 NULL）"
    )
    assert cols["device_id"][5] == 0 and cols["device_id"][3] == 0, "device_id 应为普通 NULL 遗留列"
    assert "ck_api_keys_owner_domain" in sql_text
    assert ("users", "user_id") in fks, "user_id 缺 FK → users"
    assert ("devices", "device_id") in fks, "遗留 device_id FK → devices 应保留"


def test_alembic_idempotency_pk_rebuilt_and_legacy_unique_kept(
    alembic_env: tuple[Config, Path],
) -> None:
    """P3-T2：idempotency_keys 主键 (user_id, path, idempotency_key)，遗留 UNIQUE 保留。"""
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
    assert cols["user_id"][3] == 0, "user_id 主键可空（旧行保留）"
    assert cols["device_id"][3] == 0, "device_id 应为 NULL 遗留列"
    assert {"device_id", "path", "idempotency_key"} in unique_colsets, "遗留唯一约束应保留"
    assert "ck_idempotency_keys_owner_domain" in sql_text


def test_review_events_user_client_unique_added_legacy_kept(
    alembic_env: tuple[Config, Path],
) -> None:
    """P3-T2：review_events 另加 UNIQUE (user_id, client_event_id)，原 UNIQUE 保留。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        unique_colsets = _unique_constraint_column_sets(conn, "review_events")
    assert {"device_id", "client_event_id"} in unique_colsets, "原 UNIQUE 应保留"
    assert {"user_id", "client_event_id"} in unique_colsets, "另加 UNIQUE 应存在"


def test_alembic_api_keys_device_unique_added(alembic_env: tuple[Config, Path]) -> None:
    """P4-5（P4 跟进 a）：api_keys 补回 UNIQUE (device_id)（origin='u' 列集 {device_id}）。

    SQLite UNIQUE 对 NULL 视为互异——用户域行 device_id NULL 多行不冲突；遗留设备域
    同 device_id 第二行违反唯一约束（防重兜底）。
    """
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        assert {"device_id"} in _unique_constraint_column_sets(conn, "api_keys")
        # 用户域行 device_id NULL：两行并存（多 NULL 不冲突）
        for uid in ("u1", "u2"):
            conn.execute(
                text(
                    "INSERT INTO users (user_id, username, password_hash, created_at, updated_at)"
                    " VALUES (:u, :n, 'hash', '2026-01-01T00:00:00.000Z',"
                    " '2026-01-01T00:00:00.000Z')"
                ),
                {"u": uid, "n": uid},
            )
            conn.execute(
                text(
                    "INSERT INTO api_keys (user_id, encrypted_key, status, masked_key, updated_at)"
                    " VALUES (:u, 'enc', 'AVAILABLE', 'sk-****', '2026-01-01T00:00:00.000Z')"
                ),
                {"u": uid},
            )
        # 遗留设备域防重：同 device_id 第二行违反 UNIQUE
        conn.execute(
            text(
                "INSERT INTO devices (device_id, first_seen_ip, user_agent, last_active_at,"
                " created_at)"
                " VALUES ('dev1', NULL, NULL, NULL, '2026-01-01T00:00:00.000Z')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO api_keys (device_id, encrypted_key, status, masked_key, updated_at)"
                " VALUES ('dev1', 'enc-1', 'AVAILABLE', 'sk-****abcd',"
                " '2026-01-01T00:00:00.000Z')"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_keys (device_id, encrypted_key, status, masked_key,"
                " updated_at)"
                " VALUES ('dev1', 'enc-2', 'AVAILABLE', 'sk-****efgh',"
                " '2026-01-01T00:00:00.000Z')"
            )
        )


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


def test_alembic_legacy_rows_preserved_after_upgrade(alembic_env: tuple[Config, Path]) -> None:
    """P3-T2：旧库副本 upgrade 后旧行 device_id 原值保留、user_id 为 NULL、行数守恒。"""
    config, db_path = alembic_env
    _upgrade_legacy_db_with_rows(config, db_path)
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM devices")).scalar() == 1
        for table in _OWNER_TABLES:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 1, f"{table} 行数应守恒（1），实际 {count}"
            row = conn.execute(text(f"SELECT device_id, user_id FROM {table}")).one()
            assert row == ("dev1", None), f"{table} 旧行 device_id 应原值保留、user_id 为 NULL"


def test_alembic_downgrade_fail_closed_with_user_data(alembic_env: tuple[Config, Path]) -> None:
    """P3-T2：存在用户域数据时 downgrade 在任何 DDL/DML 前拒绝（fail closed），表结构未变。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
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
    """P3-T2：空库往返与纯旧 device 域数据副本 upgrade→downgrade 成功（旧行保留）；
    P4 跟进 b 起含 ddc6 层 downgrade 带旧行 CI 守卫（T1 手工验证固化）。"""
    config, db_path = alembic_env
    # 空库 upgrade → downgrade → upgrade 往返
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    # 纯旧 device 域数据副本：回到旧库基线 → 插旧行 → upgrade → 只降本 revision（旧行保留）
    command.downgrade(config, "2a391e994f93")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        for sql in _LEGACY_INSERT_SQLS:
            conn.execute(text(sql))
    command.upgrade(config, "head")
    command.downgrade(config, "ddc6f34e30b8")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM api_keys")).scalar() == 1
        assert conn.execute(text("SELECT device_id FROM api_keys")).one() == ("dev1",)
        assert conn.execute(text("SELECT COUNT(*) FROM idempotency_keys")).scalar() == 1
        assert conn.execute(text("SELECT device_id FROM idempotency_keys")).one() == ("dev1",)
    # P4 跟进 b：ddc6 层 downgrade 带旧行 CI 守卫（T1 手工验证固化）——
    # 继续降回 v2.1 基线（ddc6f34e30b8 → 2a391e994f93），旧行 device_id 原值保留、行数守恒
    command.downgrade(config, "2a391e994f93")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM api_keys")).scalar() == 1
        assert conn.execute(text("SELECT device_id FROM api_keys")).one() == ("dev1",)
        assert conn.execute(text("SELECT COUNT(*) FROM idempotency_keys")).scalar() == 1
        assert conn.execute(text("SELECT device_id FROM idempotency_keys")).one() == ("dev1",)
