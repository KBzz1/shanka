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
from tests.conftest import auth_headers


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


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _make_deck_card(client: TestClient, user: dict[str, str]) -> tuple[str, str]:
    deck_id = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()}).json()[
        "deck_id"
    ]
    card_id = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**user, **_idem()}
    ).json()["card_id"]
    return deck_id, card_id


def test_review_api_queue_returns_due_card(client: TestClient) -> None:
    user = _user(client)
    deck_id, card_id = _make_deck_card(client, user)
    resp = client.get(f"/decks/{deck_id}/review", headers=user)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["card_id"] == card_id  # ReviewQueueItem 平铺：卡字段同层
    assert items[0]["review_state"]["state"] == "NEW"


def test_review_api_submit_returns_updated_state(client: TestClient) -> None:
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_id"] == card_id
    assert body["reps"] == 1
    assert body["last_rating"] == "GOOD"


def test_review_api_idempotency_key_replays(client: TestClient) -> None:
    """同 key 同 body 两次提交：键层重放首次完整快照，业务副作用仅一次。"""
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    headers = {**user, **_idem()}
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


def test_review_api_client_event_id_dedup_key_differs(client: TestClient) -> None:
    """实际场景带 Idempotency-Key 且仅 key 不同：键层不介入（key 不同），
    client_event_id 兜底生效 → 事件不重复；重放响应 = 当前 review_state 视图（R-12 完整口径）。"""
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    client_event = str(uuid.uuid4())
    payload = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": client_event,
        "device_timezone": "Asia/Shanghai",
    }
    r1 = client.post("/review-events", json=payload, headers={**user, **_idem()})
    r2 = client.post("/review-events", json=payload, headers={**user, **_idem()})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()  # R-12 兜底重放完整口径：响应 = 当前 review_state 视图
    assert r2.json()["reps"] == 1  # client_event_id 兜底重放当前视图，不重复计数


def test_review_api_client_event_conflict(client: TestClient) -> None:
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    client_event = str(uuid.uuid4())
    payload_good = {
        "card_id": card_id,
        "rating": "GOOD",
        "client_event_id": client_event,
        "device_timezone": "Asia/Shanghai",
    }
    r1 = client.post("/review-events", json=payload_good, headers={**user, **_idem()})
    assert r1.status_code == 200  # 首 POST 正常成功，冲突来自第二 POST 的 client_event_id 复用
    payload_again = {**payload_good, "rating": "AGAIN"}
    resp = client.post("/review-events", json=payload_again, headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVIEW_EVENT_CONFLICT"


def test_review_api_invalid_rating_400(client: TestClient) -> None:
    """rating 为 str（schema 不 Literal 拦截）→ service 内校验抛 REVIEW_EVENT_INVALID 400。"""
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "MAYBE",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "REVIEW_EVENT_INVALID"


def test_review_api_invalid_timezone_400(client: TestClient) -> None:
    """M-3（final review）：device_timezone 非 IANA 时区 → 400 VALIDATION_ERROR（契约第 7 章）。"""
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    resp = client.post(
        "/review-events",
        json={
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Not/AZone",
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_review_api_cross_user_404(client: TestClient) -> None:
    """跨用户提交评级：卡归属校验 → CARD_NOT_FOUND（用独立 other user 头）。"""
    user = _user(client)
    _, card_id = _make_deck_card(client, user)
    other = _user(client, "user2", "pass-2222")
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
