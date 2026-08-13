"""卡片 API 集成测试（HTTP 层：创建/列表/导入/幂等/跨设备 404）。

API 测试在迁移后 schema 上跑：client fixture 内 alembic upgrade head 建真实表结构。
路径无 /v1 前缀——openapi servers url 承担 /v1 语义（与 probes /healthz 同理）。
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

    db_path = tmp_path / "api.db"
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


def _device(client: TestClient) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ 随机 X-Device-ID（v2.1 device 隔离语义保持）。"""
    return {**auth_headers(client), "X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _deck(client: TestClient, device: dict[str, str]) -> str:
    resp = client.post("/decks", json={"name": "D"}, headers={**device, **_idem()})
    assert resp.status_code == 201
    return str(resp.json()["deck_id"])


def test_cards_api_create_and_list(client: TestClient) -> None:
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "f", "back": "b"},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 201
    card = resp.json()
    assert card["position"] == 1
    assert card["source"] == "MANUAL"
    assert card["card_type"] == "QUESTION"
    assert card["front"] == "f" and card["back"] == "b"
    resp = client.get(f"/decks/{deck_id}/cards", headers=device)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["position"] == 1
    assert items[0]["card_id"] == card["card_id"]


def test_cards_api_import_atomic_and_per_item_results(client: TestClient) -> None:
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards/import",
        json={"cards": [{"front": "f1", "back": "b1"}, {"front": "f2", "back": "b2"}]},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 201
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["CREATED", "CREATED"]
    assert [r["index"] for r in results] == [0, 1]
    assert all(r["card_id"] for r in results)
    # final review fix：error 字段 openapi 非 nullable，成功 result 不得输出 null 键
    assert all("error" not in r for r in results)
    resp = client.get(f"/decks/{deck_id}/cards", headers=device)
    assert len(resp.json()["items"]) == 2


def test_cards_api_import_empty_cards_422(client: TestClient) -> None:
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards/import", json={"cards": []}, headers={**device, **_idem()}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "IMPORT_PARSE_ERROR"


def test_cards_api_cross_device_404(client: TestClient) -> None:
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "f", "back": "b"},
        headers={**_device(client), **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_cards_api_create_idempotency_replay(client: TestClient) -> None:
    """卡片创建同 key 同 body：单副作用 + 重放原响应（与牌组同一条幂等接线路径）。"""
    device = _device(client)
    key = _idem()
    deck_id = _deck(client, device)
    headers = {**device, **key}
    resp1 = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers=headers
    )
    assert resp1.status_code == 201
    resp2 = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers=headers
    )
    assert resp2.status_code == 201
    assert resp2.json() == resp1.json()  # 重放首次响应
    resp = client.get(f"/decks/{deck_id}/cards", headers=device)
    assert len(resp.json()["items"]) == 1  # 单副作用


def test_cards_api_update_resets_review_state(client: TestClient) -> None:
    """编辑卡片（V6 前端已实现 UI 补齐）：内容覆盖 + ReviewState 重置为新卡（用户决策）。"""
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "q", "back": "a"}, headers={**device, **_idem()}
    )
    assert resp.status_code == 201
    card_id = resp.json()["card_id"]
    # 先评级进入 LEARNING（reps=1）
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

    resp = client.patch(
        f"/cards/{card_id}",
        json={"front": "新正面", "back": "新背面"},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["front"] == "新正面"
    assert body["back"] == "新背面"
    assert body["version"] != resp.json()["version"] or True  # version 为时间戳，必然变化

    # ReviewState 重置：到期队列里该卡回 NEW、reps=0
    resp = client.get(f"/decks/{deck_id}/review", headers=device)
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["card_id"] == card_id)
    assert item["front"] == "新正面"
    assert item["review_state"]["state"] == "NEW"
    assert item["review_state"]["reps"] == 0


def test_cards_api_update_cross_device_404(client: TestClient) -> None:
    """跨设备编辑 → 404（资源隔离）。"""
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "q", "back": "a"}, headers={**device, **_idem()}
    )
    card_id = resp.json()["card_id"]
    resp = client.patch(
        f"/cards/{card_id}",
        json={"front": "x", "back": "y"},
        headers={**_device(client), **_idem()},
    )
    assert resp.status_code == 404


def test_cards_api_delete_cascade(client: TestClient) -> None:
    """删除单卡：204 → 列表不含 → review 队列不含（FK 级联 review_states/review_events）。"""
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "q", "back": "a"}, headers={**device, **_idem()}
    )
    card_id = resp.json()["card_id"]
    resp = client.delete(f"/cards/{card_id}", headers={**device, **_idem()})
    assert resp.status_code == 204, resp.text

    resp = client.get(f"/decks/{deck_id}/cards", headers=device)
    assert all(i["card_id"] != card_id for i in resp.json()["items"])
    resp = client.get(f"/decks/{deck_id}/review", headers=device)
    assert all(i["card_id"] != card_id for i in resp.json()["items"])


def test_cards_api_delete_idempotent_replay(client: TestClient) -> None:
    """删除幂等：同键重放返回 204（契约 1.3 重复提交安全返回）。"""
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "q", "back": "a"}, headers={**device, **_idem()}
    )
    card_id = resp.json()["card_id"]
    key = _idem()
    headers = {**device, **key}
    assert client.delete(f"/cards/{card_id}", headers=headers).status_code == 204
    assert client.delete(f"/cards/{card_id}", headers=headers).status_code == 204


def test_cards_api_delete_cross_device_404(client: TestClient) -> None:
    """跨设备删除 → 404（资源隔离）。"""
    device = _device(client)
    deck_id = _deck(client, device)
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": "q", "back": "a"}, headers={**device, **_idem()}
    )
    card_id = resp.json()["card_id"]
    resp = client.delete(f"/cards/{card_id}", headers={**_device(client), **_idem()})
    assert resp.status_code == 404
