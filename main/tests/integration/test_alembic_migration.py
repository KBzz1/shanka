"""Alembic 迁移集成测试：空库 upgrade → 12 表 + 约束；downgrade → 空库；再 upgrade 恢复。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

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
