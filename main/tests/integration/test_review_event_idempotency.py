"""离线补传幂等判别（offline-foundation-v1）：真实失效模型的 API/数据库级证据。

双幂等（structure-contract 1.3）：Idempotency-Key 键层命中优先（重放首次完整快照）；
client_event_id 兜底（UNIQUE(user_id, client_event_id)：重放当前 ReviewState 视图，
同 client_event_id 配不同 card/rating → 409 REVIEW_EVENT_CONFLICT）。本文件补足
「响应丢失 / 新幂等键 / 进程重启 / 并发重复提交 / 跨账号」判别证据；
已有基础用例见 test_review_api.py。
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

# 与 test_review_api.py 同策略：IP 总闸门对测试放开（避免正常请求计数干扰语义断言）
_IP_TEST_LIMIT = 1000
_DB_NAME = "review_event_idem.db"


@pytest.fixture
def client_factory(tmp_path: Path) -> Iterator[Callable[[], TestClient]]:
    """每个实例 = 独立 create_app（独立 engine/session_factory）→ 模拟独立服务进程。

    所有实例指向同一迁移后临时 DB 文件（进程重启后旧数据/会话仍可读的等价物）。
    """
    db_path = tmp_path / _DB_NAME

    def _upgrade() -> None:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
        command.upgrade(cfg, "head")

    _upgrade()
    clients: list[TestClient] = []

    def make_client() -> TestClient:
        settings = Settings(
            database_url=f"sqlite:///{db_path}",
            storage_path=tmp_path / "storage",
            rate_limit_ip_per_second=_IP_TEST_LIMIT,
        )
        client = TestClient(create_app(settings))
        client.__enter__()
        clients.append(client)
        return client

    try:
        yield make_client
    finally:
        for c in clients:
            c.__exit__(None, None, None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / _DB_NAME


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


def _event_payload(card_id: str, client_event_id: str, rating: str = "GOOD") -> dict[str, str]:
    return {"card_id": card_id, "rating": rating, "client_event_id": client_event_id}


def _count_events(db_path: Path, client_event_id: str | None = None) -> int:
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        if client_event_id is None:
            return int(conn.execute(text("SELECT count(*) FROM review_events")).scalar() or 0)
        row = conn.execute(
            text("SELECT count(*) FROM review_events WHERE client_event_id = :cev"),
            {"cev": client_event_id},
        ).scalar()
    return int(row or 0)


def _concurrent_post(
    client: TestClient, payload: dict[str, str], user: dict[str, str]
) -> tuple[list[int], list[str]]:
    """双线程同时 POST 同一事件（不同 Idempotency-Key）；返回 (状态码列表, 异常列表)。"""
    results: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(2)

    def worker() -> None:
        try:
            start.wait(timeout=10)
            resp = client.post("/review-events", json=payload, headers={**user, **_idem()})
            with lock:
                results.append(resp.status_code)
        except Exception as exc:  # 线程异常直接暴露（与 test_idempotency_primitive 同模式）
            with lock:
                errors.append(repr(exc))
            raise

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_review_event_same_key_same_body_replays_once(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 1：同 Idempotency-Key + 同 body 重放 → 键层命中，事件只落库一次。"""
    client = client_factory()
    user = auth_headers(client)
    _, card_id = _make_deck_card(client, user)
    headers = {**user, **_idem()}
    payload = _event_payload(card_id, str(uuid.uuid4()))
    r1 = client.post("/review-events", json=payload, headers=headers)
    r2 = client.post("/review-events", json=payload, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()  # 键层重放首次完整快照
    assert r2.json()["review_state"]["reps"] == 1  # 未重复执行
    assert _count_events(db_path, payload["client_event_id"]) == 1


def test_review_event_different_key_same_client_event_dedups(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 2：不同 Idempotency-Key + 同 client_event_id → 键层不命中，兜底重放：
    响应 = 当前 ReviewState 视图（R-12），事件不重复、reps 不重复计数。"""
    client = client_factory()
    user = auth_headers(client)
    _, card_id = _make_deck_card(client, user)
    cev = str(uuid.uuid4())
    payload = _event_payload(card_id, cev)
    r1 = client.post("/review-events", json=payload, headers={**user, **_idem()})
    r2 = client.post("/review-events", json=payload, headers={**user, **_idem()})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert r2.json()["review_state"]["reps"] == 1
    assert _count_events(db_path, cev) == 1


def test_review_event_same_client_event_different_card_conflicts(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 3：同 client_event_id 配不同 card → 409 REVIEW_EVENT_CONFLICT（不落新事件）。"""
    client = client_factory()
    user = auth_headers(client)
    deck_id, card1 = _make_deck_card(client, user)
    card2 = client.post(
        f"/decks/{deck_id}/cards", json={"front": "g", "back": "h"}, headers={**user, **_idem()}
    ).json()["card_id"]
    cev = str(uuid.uuid4())
    r1 = client.post("/review-events", json=_event_payload(card1, cev), headers={**user, **_idem()})
    assert r1.status_code == 200
    conflict = client.post(
        "/review-events", json=_event_payload(card2, cev), headers={**user, **_idem()}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REVIEW_EVENT_CONFLICT"
    assert _count_events(db_path, cev) == 1  # 冲突不产生第二行


def test_review_event_same_client_event_different_rating_conflicts(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 3b：同 client_event_id 配不同 rating（同 card）→ 409 REVIEW_EVENT_CONFLICT。

    client_event_id 语义 = 恰好一次学习动作（1.3）：换 rating 的重复提交不得被当作
    重放吞掉，也不得落第二行（reps/lapses 不变）。
    """
    client = client_factory()
    user = auth_headers(client)
    _, card_id = _make_deck_card(client, user)
    cev = str(uuid.uuid4())
    r1 = client.post(
        "/review-events", json=_event_payload(card_id, cev, rating="GOOD"), headers={**user, **_idem()}
    )
    assert r1.status_code == 200
    conflict = client.post(
        "/review-events", json=_event_payload(card_id, cev, rating="AGAIN"), headers={**user, **_idem()}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REVIEW_EVENT_CONFLICT"
    assert _count_events(db_path, cev) == 1
    # 冲突未执行：review_state 保持首次 GOOD 的结果（reps=1、lapses=0）
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT reps, lapses, last_rating FROM review_states WHERE card_id = :c"),
            {"c": card_id},
        ).one()
    assert tuple(row) == (1, 0, "GOOD")


def test_review_event_replay_after_process_restart(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 4：独立 app/session 实例（模拟进程重启）指向同一 DB 重放：
    新进程换新 Idempotency-Key 提交同 client_event_id → 兜底重放（200 同响应），
    事件行数仍为 1（数据库权威）。"""
    client1 = client_factory()
    user = auth_headers(client1)
    _, card_id = _make_deck_card(client1, user)
    token = user["Authorization"]  # 会话持于 DB：重启后同 token 仍可认证
    cev = str(uuid.uuid4())
    payload = _event_payload(card_id, cev)
    r1 = client1.post("/review-events", json=payload, headers={**user, **_idem()})
    assert r1.status_code == 200
    client2 = client_factory()
    r2 = client2.post("/review-events", json=payload, headers={"Authorization": token, **_idem()})
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert r2.json()["review_state"]["reps"] == 1  # 重启后重放不产生第二次计数
    assert _count_events(db_path, cev) == 1


def test_review_event_concurrent_duplicate_single_effect(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 5：同进程并发提交同一事件（不同 Idempotency-Key —— 客户端重启丢键后重试的
    形态之一）：恰好一个 ReviewEvent；reps/lapses 只计一次（无 500、无约束泄漏）。"""
    client = client_factory()
    user = auth_headers(client)
    _, card_id = _make_deck_card(client, user)
    cev = str(uuid.uuid4())
    payload = _event_payload(card_id, cev, rating="AGAIN")
    results, errors = _concurrent_post(client, payload, user)
    assert errors == []
    assert results == [200, 200]  # 一个首次、一个重放，无 429/500
    assert _count_events(db_path, cev) == 1
    # 服务行为视图（数据库权威）：reps/lapses 只计一次；评级后卡 not due，故查库不能查队列
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT reps, lapses, last_rating FROM review_states WHERE card_id = :c"),
            {"c": card_id},
        ).one()
    assert tuple(row) == (1, 1, "AGAIN")  # AGAIN 提交恰好一次：reps +1、lapses +1


def test_review_event_concurrent_stats_single_increment(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 5 统计侧：并发重复提交后 dashboard 仅反映一次事件（无重复计数）。"""
    client = client_factory()
    user = auth_headers(client)
    _, card_id = _make_deck_card(client, user)
    cev = str(uuid.uuid4())
    payload = _event_payload(card_id, cev)
    results, errors = _concurrent_post(client, payload, user)
    assert errors == []
    assert results == [200, 200]
    assert _count_events(db_path, cev) == 1
    dash = client.get("/stats/dashboard", headers=user).json()
    assert dash["weekly_total"] == 1  # 统计只增加一次
    assert dash["weekly_completed_count"] == 1
    assert dash["streak_days"] == 1


def test_review_event_client_event_id_isolated_per_user(
    client_factory: Callable[[], TestClient], db_path: Path
) -> None:
    """场景 6：账号 A/B 可用相同 client_event_id（UNIQUE(user_id, client_event_id) 域隔离）。"""
    client = client_factory()
    user_a = auth_headers(client, "alice", "secret-pass-1")
    user_b = auth_headers(client, "bob", "secret-pass-2")
    _, card_a = _make_deck_card(client, user_a)
    _, card_b = _make_deck_card(client, user_b)
    cev = str(uuid.uuid4())
    ra = client.post(
        "/review-events", json=_event_payload(card_a, cev), headers={**user_a, **_idem()}
    )
    rb = client.post(
        "/review-events", json=_event_payload(card_b, cev), headers={**user_b, **_idem()}
    )
    assert ra.status_code == 200 and rb.status_code == 200
    assert _count_events(db_path, cev) == 2  # 每账号各自一行（不影响统计隔离）
