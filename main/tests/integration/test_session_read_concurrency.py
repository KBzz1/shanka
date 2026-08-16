"""读事务并发语义集成测试：读不得抢写锁（500 根因修复回归守卫）。

背景：engine 级 begin 事件曾对所有事务（含只读）执行 BEGIN IMMEDIATE——
并发请求在 auth 中间件等读路径互相排队撞 5s busy timeout（OperationalError:
database is locked → 500）。修复后读事务走普通 BEGIN（WAL 读快照不阻塞写者），
写-写竞争由 WAL + busy_timeout 序列化。本文件锁死"读不等写锁"这一语义。
"""

import sqlite3
import threading
import time
from pathlib import Path

from sqlalchemy import text

from infra.db.session import create_db_engine, create_session_factory


def test_read_does_not_block_on_held_write_lock(tmp_path: Path) -> None:
    """写者持有写锁（BEGIN IMMEDIATE）时，并发读应立即返回 WAL 快照，而非排队等锁。

    旧实现（engine 级 BEGIN IMMEDIATE）：读事务同样拿写锁，此处会阻塞到写者
    commit（≥2s）甚至 5s busy timeout 抛 database is locked。
    """
    db_path = tmp_path / "concurrent.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = create_session_factory(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))

    writer_ready = threading.Event()
    writer_release = threading.Event()

    def hold_write_lock() -> None:
        raw = sqlite3.connect(db_path, timeout=5)
        raw.execute("BEGIN IMMEDIATE")
        writer_ready.set()
        writer_release.wait(timeout=5)
        raw.execute("COMMIT")
        raw.close()

    thread = threading.Thread(target=hold_write_lock)
    thread.start()
    assert writer_ready.wait(timeout=2), "写者未能在预期时间内拿到写锁"

    start = time.monotonic()
    with factory() as session:
        value = session.execute(text("SELECT v FROM t WHERE id = 1")).scalar()
    elapsed = time.monotonic() - start

    writer_release.set()
    thread.join(timeout=5)

    assert value == "a"  # WAL 读快照仍能看到已提交数据
    assert elapsed < 1.0, f"读事务阻塞等待写锁 {elapsed:.2f}s——读又抢写锁了"
