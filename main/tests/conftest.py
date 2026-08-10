"""共享测试基座（Progress F0）：隔离 Settings、临时 DB/存储、TestClient、可控时钟。

隔离测试配置：fixture 一律显式构造 Settings（临时目录），不加载任何 .env/环境配置；
时钟经 infra.clock 注入，服务代码只能通过 Clock 接口取时间。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.config import Settings
from app.main import create_app
from infra.clock import FrozenClock

REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/conftest.py → 仓库根


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}", storage_path=tmp_path / "storage"
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC))


@pytest.fixture
def db_engine(tmp_path: Path) -> Engine:
    """迁移后的真实 schema（alembic upgrade head），供 V1+ integration 测试使用。

    API 测试须在迁移后 schema 上跑：HTTP 测试文件内的 client fixture 各自
    alembic upgrade 到独立临时库（同一 REPO_ROOT 路径来源）。
    """
    from alembic import command
    from alembic.config import Config

    from infra.db.session import create_db_engine

    db_path = tmp_path / "migrated.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return create_db_engine(f"sqlite:///{db_path}")
