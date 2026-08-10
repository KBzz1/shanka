"""复习 API 集成测试：到期队列/评级/双幂等/隔离（迁移 schema + HTTP）。

路径无 /v1 前缀——openapi servers url 承担 /v1 语义（与 cards/decks 测试同理）。
双幂等（1.3）：Idempotency-Key 层（execute_idempotent 全快照重放）+ client_event_id
兜底（service biz 内，重放当前 review_state 视图，R-12 口径）。
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "review_api.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _make_deck_card(client: TestClient, device: dict[str, str]) -> tuple[str, str]:
    deck_id = client.post("/decks", json={"name": "D"}, headers={**device, **_idem()}).json()[
        "deck_id"
    ]
    card_id = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**device, **_idem()}
    ).json()["card_id"]
    return deck_id, card_id


def test_review_api_queue_returns_due_card(client: TestClient) -> None:
    device = _device()
    deck_id, card_id = _make_deck_card(client, device)
    resp = client.get(f"/decks/{deck_id}/review", headers=device)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["card_id"] == card_id  # ReviewQueueItem 平铺：卡字段同层
    assert items[0]["review_state"]["state"] == "NEW"


def test_review_api_submit_returns_updated_state(client: TestClient) -> None:
    device = _device()
    _, card_id = _make_deck_card(client, device)
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**device, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_id"] == card_id
    assert body["reps"] == 1
    assert body["last_rating"] == "GOOD"


def test_review_api_idempotency_key_replays(client: TestClient) -> None:
    """同 key 同 body 两次提交：键层重放首次完整快照，业务副作用仅一次。"""
    device = _device()
    _, card_id = _make_deck_card(client, device)
    headers = {**device, **_idem()}
    payload = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": str(uuid.uuid4()),
        "device_timezone": "Asia/Shanghai",
    }
    resp1 = client.post("/review-events", json=payload, headers=headers)
    resp2 = client.post("/review-events", json=payload, headers=headers)
    assert resp1.status_code == 200 and resp2.status_code == 200
    assert resp1.json() == resp2.json()  # 单事件（幂等键层重放首次完整快照）
    assert resp2.json()["reps"] == 1  # 未重复执行


def test_review_api_client_event_id_dedup_without_idem_key(client: TestClient) -> None:
    """无 Idempotency-Key？契约要求评级带幂等键（openapi IdempotencyKey 参数）——
    本用例验证带 key 时 client_event_id 兜底仍生效：同 key 异 body 会 409（key 层），
    故用 client_event_id 相同但 key 不同 → 事件不重复。"""
    device = _device()
    _, card_id = _make_deck_card(client, device)
    client_event = str(uuid.uuid4())
    payload = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": client_event,
        "device_timezone": "Asia/Shanghai",
    }
    r1 = client.post("/review-events", json=payload, headers={**device, **_idem()})
    r2 = client.post("/review-events", json=payload, headers={**device, **_idem()})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["reps"] == 1  # client_event_id 兜底重放当前视图，不重复计数


def test_review_api_client_event_conflict(client: TestClient) -> None:
    device = _device()
    _, card_id = _make_deck_card(client, device)
    client_event = str(uuid.uuid4())
    payload_good = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": client_event,
        "device_timezone": "Asia/Shanghai",
    }
    client.post("/review-events", json=payload_good, headers={**device, **_idem()})
    payload_again = {**payload_good, "rating": "AGAIN"}
    resp = client.post("/review-events", json=payload_again, headers={**device, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_EVENT_CONFLICT"


def test_review_api_invalid_rating_400(client: TestClient) -> None:
    """rating 为 str（schema 不 Literal 拦截）→ service 内校验抛 REVIEW_EVENT_INVALID 400。"""
    device = _device()
    _, card_id = _make_deck_card(client, device)
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "MAYBE",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**device, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "REVIEW_EVENT_INVALID"


def test_review_api_cross_device_404(client: TestClient) -> None:
    """跨设备提交评级：卡归属校验 → CARD_NOT_FOUND（用独立 other device 头）。"""
    device = _device()
    _, card_id = _make_deck_card(client, device)
    other = _device()
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**other, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"
