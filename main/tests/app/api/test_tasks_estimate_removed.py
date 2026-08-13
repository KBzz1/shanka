"""删除回归测试（Task 13）：/tasks/estimate 端点与 token 估算模块已随链路移除。

Bearer 认证先于路由运行（main.py 中间件运行序），带合法 Bearer 才到达路由匹配层。
brief 指定 404，但 /tasks/{task_id}（GET 详情）路径模式仍匹配 /tasks/estimate 的
路径段，Starlette 对"路径存在但方法不存在"返回 405 ——405 即端点已删的运行时事实
（404 需 GET 详情路由也不存在才能出现）；断言收 {404, 405} 集合（两种运行时事实
都证明端点已删除，防御路由结构变化）。
token_estimator 模块随链路删除：import 必须 ModuleNotFoundError。
"""

import importlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import Base
from infra.db.session import create_db_engine, create_session_factory
from tests.conftest import auth_headers


@pytest.fixture
def session_factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """预建全表 schema：Bearer 经 auth 中间件 → 需 users/auth_sessions 表。"""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'estimate_removed.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)


def test_estimate_endpoint_removed(tmp_path: Path, session_factory: sessionmaker[Session]) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'estimate_removed.db'}",
        storage_path=tmp_path / "storage",
    )
    client = TestClient(create_app(settings))
    # P4-4 起仅 Bearer（create_all 已含 users/auth_sessions 表）
    headers = auth_headers(client)
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [str(uuid.uuid4())],
            "generation_config": {
                "quantity_tendency": "COMPACT",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers=headers,
    )
    # brief 期望 404;运行时为 405:/tasks/{task_id}(GET) 路径模式仍匹配 /tasks/estimate
    assert resp.status_code in {404, 405}


def test_token_estimator_module_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.generation.token_estimator")
