"""infra.db.session 引擎集成测试：空测试库创建、WAL/外键（database-design 0）。"""

from pathlib import Path

from sqlalchemy import text

from infra.db.session import create_db_engine


def test_session_engine_creates_empty_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
        tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    assert db_path.exists()
    assert tables == []


def test_session_engine_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
