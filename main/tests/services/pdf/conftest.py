"""tests/services/pdf 层测试基座：tmp sqlite 全表建库 + session/storage fixture。

构造方式与 tests/integration/test_pdf_scanner.py 同款（create_db_engine 统一
配置 PRAGMA foreign_keys=ON，级联删除可验证）。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from infra.db.models import Base
from infra.db.session import create_db_engine, create_session_factory
from infra.storage.local import LocalStorage


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'chunks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path / "storage")
