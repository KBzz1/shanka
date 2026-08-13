"""验收测试：AC-10 复习与统计联调（PRD；迁移 schema + HTTP）。

映射：
- AC-10-1 到期队列仅含到期卡；评级后状态/进度/事件正确更新；client_event_id 重试不重复计数
- AC-10-2 看板展示真实周活动/总数/变化率/正确率/streak/掌握卡数；空态非示例值

补覆盖（V2-T3 审查裁决）：GET 队列 items[0] 断言 review_state 字段形状（handler dict 直传，
响应形状由测试断言；schema 一致性由守卫文件覆盖）。
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac10.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        # 验收用例单设备请求密集：限流阈值属可运维调优项（契约 1.6），测试显式调高
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_acceptance_ac10_review_workflow(client: TestClient) -> None:
    """AC-10-1：到期队列仅含到期卡；评级后状态/进度/事件正确更新；client_event_id 重试不重复计数。"""
    device = _user(client)
    resp = client.post("/decks", json={"name": "D"}, headers={**device, **_idem()})
    assert resp.status_code == 201
    deck_id = resp.json()["deck_id"]
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**device, **_idem()}
    )
    assert resp.status_code == 201
    card_id = resp.json()["card_id"]
    # 到期队列含新卡（新卡初始 due=now → 恒到期），items[0] 为 ReviewQueueItem 平铺形状
    resp = client.get(f"/decks/{deck_id}/review", headers=device)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert "review_state" in items[0]
    assert items[0]["review_state"]["state"] == "NEW"
    assert items[0]["review_state"]["reps"] == 0
    # 评级 GOOD → 状态/进度/事件更新
    client_event = str(uuid.uuid4())
    payload = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": client_event,
        "device_timezone": "Asia/Shanghai",
    }
    resp = client.post("/review-events", json=payload, headers={**device, **_idem()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "LEARNING"  # 落库统一大写（契约 3.10）
    assert body["reps"] == 1
    # 离线重试同一 client_event_id（不同幂等键）→ 不重复计数
    resp = client.post("/review-events", json=payload, headers={**device, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["reps"] == 1
    # 牌组进度 review_count=1
    resp = client.get(f"/decks/{deck_id}", headers=device)
    assert resp.json()["review_count"] == 1
    # 到期队列不再含已评级卡（GOOD 后 due 推到未来，队列按 due<=now 过滤）
    resp = client.get(f"/decks/{deck_id}/review", headers=device)
    assert resp.json()["items"] == []


def test_acceptance_ac10_dashboard_real_data(client: TestClient) -> None:
    """AC-10-2：看板展示真实周活动/总数/变化率/正确率/streak/掌握卡数；空态非示例值。"""
    device = _user(client)
    resp = client.post("/decks", json={"name": "D"}, headers={**device, **_idem()})
    deck_id = resp.json()["deck_id"]
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**device, **_idem()}
    )
    card_id = resp.json()["card_id"]
    payload = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": str(uuid.uuid4()),
        "device_timezone": "Asia/Shanghai",
    }
    client.post("/review-events", json=payload, headers={**device, **_idem()})
    resp = client.get(
        "/stats/dashboard", params={"timezone": "Asia/Shanghai", "weekly_goal": 50}, headers=device
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_data"] is True
    assert body["weekly_total"] == 1
    assert sum(body["weekly_activity"]) == 1  # 周活动每日计数合计 = 周总数
    assert body["weekly_goal"] == 50
    assert body["weekly_goal_progress"] == 0.02  # 1/50
    assert body["recall_accuracy"] == 1.0  # 周内 1 GOOD / 1
    assert body["streak_days"] >= 1  # 今天有事件（事件 reviewed_at = 服务端真实 now）
    assert body["period"]["week_ordinal"] >= 1
    # 空态（新用户）：非示例值，weekly_goal 未上报 → null
    empty = client.get(
        "/stats/dashboard",
        params={"timezone": "Asia/Shanghai"},
        headers=_user(client, "user2", "pass-2222"),
    )
    assert empty.status_code == 200
    empty_body = empty.json()
    assert empty_body["has_data"] is False
    assert empty_body["weekly_goal"] is None
    assert empty_body["weekly_goal_progress"] is None
    assert empty_body["recall_accuracy"] is None
