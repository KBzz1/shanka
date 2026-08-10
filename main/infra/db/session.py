"""DB 唯一入口（database-design 0 / Progress 2.5）。

连接配置（database-design 0，审核修复）：
- `PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;` 在 engine 级 connect 事件统一配置；
- SQLite 必须 `check_same_thread=False`（FastAPI 线程池复用连接）；
- 写事务 `BEGIN IMMEDIATE`（database-design §0/3）：engine 级 begin 事件统一配置，
  覆盖请求级 session 与迁移脚本（F1 接入并验证）。

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
    cursor.close()


def _begin_immediate(conn: Any) -> None:
    """database-design §0/3：写事务 BEGIN IMMEDIATE（进入即拿写锁，避免并发写 SQLITE_BUSY）。

    SQLite MVP 单写者：engine 级 begin 事件统一处理，覆盖请求级 session 与迁移脚本。
    """
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_connection)
        event.listen(engine, "begin", _begin_immediate)
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
