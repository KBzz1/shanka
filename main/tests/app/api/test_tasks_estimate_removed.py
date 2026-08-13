"""删除回归测试（Task 13）：/tasks/estimate 端点与 token 估算模块已随链路移除。

设备中间件先于路由运行（main.py 中间件运行序），带合法 X-Device-ID 才到达路由
匹配层。brief 指定 404，但 /tasks/{task_id}（GET 详情）路径模式仍匹配
/tasks/estimate 的路径段，Starlette 对"路径存在但方法不存在"返回 405 ——
405 即端点已删的运行时事实（404 需 GET 详情路由也不存在才能出现）。
token_estimator 模块随链路删除：import 必须 ModuleNotFoundError。
"""

import importlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_estimate_endpoint_removed(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'estimate_removed.db'}",
        storage_path=tmp_path / "storage",
    )
    client = TestClient(create_app(settings))
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [str(uuid.uuid4())],
            "generation_config": {
                "quantity_tendency": "COMPACT",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers={"X-Device-ID": str(uuid.uuid4())},
    )
    # brief 期望 404;运行时为 405:/tasks/{task_id}(GET) 路径模式仍匹配 /tasks/estimate
    assert resp.status_code == 405


def test_token_estimator_module_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.generation.token_estimator")
