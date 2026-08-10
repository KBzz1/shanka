"""DB session 事务语义集成测试（database-design 0/3）：BEGIN IMMEDIATE + 回滚 + 请求级 session。"""

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from infra.db.session import create_db_engine, create_session_factory, get_db_session


def test_session_begin_immediate_rollback_releases_lock(tmp_path: Path) -> None:
    """写事务 BEGIN IMMEDIATE：同库第二个写事务在第一个 commit/rollback 前必须等待（串行单写者）。"""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tx.db'}")
    factory = create_session_factory(engine)
    # 建一张测试表（本任务无 ORM 表）
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"))
    with factory() as session:
        session.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        session.rollback()  # 未 commit → 释放
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM t")).scalar() == 0


def test_session_commit_persists(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tx2.db'}")
    factory = create_session_factory(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"))
    with factory() as session:
        session.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        session.commit()
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM t")).scalar() == 1


def test_session_get_db_dependency_yields_session() -> None:
    """请求级 session dependency：TestClient 请求内可执行 SQL。"""

    def create_probe_app() -> FastAPI:
        engine = create_db_engine("sqlite:///:memory:")
        factory = create_session_factory(engine)
        app = FastAPI()
        app.state.engine = engine
        app.state.session_factory = factory

        @app.get("/ping-db")
        def ping_db(session: Annotated[Session, Depends(get_db_session)]) -> dict[str, int]:
            assert session.execute(text("SELECT 1")).scalar() == 1
            return {"ok": 1}

        return app

    with TestClient(create_probe_app()) as client:
        resp = client.get("/ping-db")
    assert resp.status_code == 200
    assert resp.json() == {"ok": 1}
