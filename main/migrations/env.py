"""Alembic 迁移环境：URL 优先级 = 命令行 -x database_url=... > 环境变量 DATABASE_URL > alembic.ini 占位。

与 ORM 共用 models.Base.metadata（database-design §0：ORM 与迁移一致）；
SQLite 连接事件（WAL/外键）在迁移连接上同样生效——
env.py 通过 infra.db.session.create_db_engine 创建 engine（其 connect/begin 事件覆盖迁移脚本）。
"""

import sys
from logging.config import fileConfig
from os import environ
from pathlib import Path

from alembic import context

# 使 main/ 可导入（alembic 从 migrations/ 启动时 cwd=main/，正常路径可导入）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# 命令行 -x database_url=... 优先
_x_args = context.get_x_argument(as_dictionary=True)
if "database_url" in _x_args:
    config.set_main_option("sqlalchemy.url", _x_args["database_url"])
elif environ.get("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", environ["DATABASE_URL"])


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from infra.db.session import create_db_engine

    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        raise RuntimeError("alembic.ini 缺少 sqlalchemy.url（应至少保留占位 URL）")
    connectable = create_db_engine(url)
    with connectable.connect() as connection:
        # database-design 7.1：SQLite 迁移全程关闭外键强制——batch 重建 FK 父表时
        # DROP 旧表会触发隐式 DELETE 并级联误删子表数据（实测：带数据旧库上
        # chapters/knowledge_points/cards 等子表行被清空）。PRAGMA foreign_keys 在
        # 事务内是 no-op，因此必须
        # 在连接建立后、任何事务开始前经底层 DBAPI cursor（与 connect 事件同路径）设置，
        # 迁移结束（外层事务已提交）再恢复。连接回到池时外键强制恢复 ON。
        raw = connection.connection.driver_connection
        assert raw is not None  # 连接已建立，DBAPI 连接必然存在
        if connection.dialect.name == "sqlite":
            raw.execute("PRAGMA foreign_keys=OFF")
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if connection.dialect.name == "sqlite":
                raw.execute("PRAGMA foreign_keys=ON")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
