"""API 层看板统计集成测试（前端联调镜像：建牌组 → 加卡 → 评级 → dashboard 计入）。

回归护栏（2026-08-11 前端联调）：真实复习事件成功后 dashboard 必须立即计入
（weekly_total=1、has_data=true）。当日前端观察到 has_data=false 的根因是其在
DELETE 牌组（级联清空卡片与复习事件）之后才查询统计，非后端缺陷；
本测试锁定"评级即计入"的正确行为与空设备空态语义。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "stats.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 双头窗口：Bearer 注册请求计入 IP 维度（连发 >5 req/s），显式调高隔离,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _headers(client: TestClient, device_id: str, key: str | None = None) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ 指定 X-Device-ID。"""
    headers = {**auth_headers(client), "X-Device-ID": device_id}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_stats_api_review_counts_into_dashboard(client: TestClient) -> None:
    """评级成功 → dashboard 计入：weekly_total=1、has_data=true、recall_accuracy=1.0。"""
    device = str(uuid.uuid4())
    # 1. 建牌组
    resp = client.post(
        "/decks", json={"name": "统计回归"}, headers=_headers(client, device, str(uuid.uuid4()))
    )
    assert resp.status_code == 201, resp.text
    deck_id = resp.json()["deck_id"]
    # 2. 手动加卡
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "q", "back": "a"},
        headers=_headers(client, device, str(uuid.uuid4())),
    )
    assert resp.status_code == 201, resp.text
    card_id = resp.json()["card_id"]
    # 3. 评级（GOOD）
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers=_headers(client, device, str(uuid.uuid4())),
    )
    assert resp.status_code == 200, resp.text
    # 4. 看板立即计入
    resp = client.get(
        "/stats/dashboard?timezone=Asia/Shanghai&weekly_goal=50", headers=_headers(client, device)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["weekly_total"] == 1
    assert body["has_data"] is True
    assert body["recall_accuracy"] == 1.0
    assert body["weekly_goal_progress"] == 0.02  # min(1/50, 1)


def test_stats_api_empty_has_data_false(client: TestClient) -> None:
    """空设备看板 has_data=false（前端空态判定）。"""
    resp = client.get(
        "/stats/dashboard?timezone=Asia/Shanghai", headers=_headers(client, str(uuid.uuid4()))
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_data"] is False
    assert body["weekly_total"] == 0
    assert body["recall_accuracy"] is None
