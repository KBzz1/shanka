"""DB 唯一入口（database-design 0 / Progress 2.5）。

连接配置（database-design 0，审核修复）：
- `PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;` 在 engine 级 connect 事件统一配置；
- SQLite 必须 `check_same_thread=False`（FastAPI 线程池复用连接）；
- 写事务 `BEGIN IMMEDIATE`（isolation_level='IMMEDIATE'）随 F1 写事务接入并验证，F0 不引入未验证配置。

时间格式唯一规范（database-design 0）：`YYYY-MM-DDTHH:MM:SS.sssZ`
（UTC、零填充、恒 3 位毫秒），由 `format_utc` 统一生成，禁止 `isoformat()` 默认输出。
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, create_engine, event


def _configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_connection)
    return engine


def format_utc(dt: datetime) -> str:
    """统一 UTC 时间格式（database-design 0）；naive datetime 拒绝（防本地时区陷阱）。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不可序列化：必须携带 UTC 时区")
    dt_utc = dt.astimezone(UTC)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"
