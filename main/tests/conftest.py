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

from app.config import Settings
from app.main import create_app
from infra.clock import FrozenClock


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
