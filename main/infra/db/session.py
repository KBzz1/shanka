"""DB 唯一入口（database-design 0 / Progress 2.5）。

连接配置（database-design 0，审核修复）：
- `PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;` 在 engine 级 connect 事件统一配置；
- SQLite 必须 `check_same_thread=False`（FastAPI 线程池复用连接）；
- 事务模式（2026-08-16 修复，database-design §0/3 偏差记录）：
  读事务走普通 BEGIN（WAL 读快照不阻塞写者）；写-写竞争由 WAL + pysqlite busy
  timeout 序列化。原「engine 级 begin 事件对每个事务发 BEGIN IMMEDIATE」会让
  auth 中间件等只读路径抢写锁，并发请求排队撞 5s busy timeout
  （OperationalError: database is locked → 500）。回归守卫：
  tests/integration/test_session_read_concurrency.py。

时间格式唯一规范（database-design 0）：`YYYY-MM-DDTHH:MM:SS.sssZ`
（UTC、零填充、恒 3 位毫秒），由 `format_utc` 统一生成，禁止 `isoformat()` 默认输出。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def _configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    # SQLite permits one writer at a time; wait briefly for an active writer instead
    # of surfacing an immediate \"database is locked\" response.
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": 5.0}
        if database_url.startswith("sqlite")
        else {},
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_connection)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_db_session(request: Request) -> Iterator[Session]:
    """请求级 session（FastAPI dependency，F1 起注入 handler）。

    事务语义归 service：本 dependency 只创建 session、请求结束关闭；
    提交/回滚由调用方（service 用例）显式控制，禁止在 infra helper 内 commit。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


def format_utc(dt: datetime) -> str:
    """统一 UTC 时间格式（database-design 0）；naive datetime 拒绝（防本地时区陷阱）。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不可序列化：必须携带 UTC 时区")
    dt_utc = dt.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"
