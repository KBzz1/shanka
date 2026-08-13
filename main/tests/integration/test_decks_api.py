"""牌组 API 集成测试（HTTP 层 + 幂等接线 + 跨设备 404 + 删除保护）。

API 测试在迁移后 schema 上跑：client fixture 内 alembic upgrade head 建真实表结构。
路径无 /v1 前缀——openapi servers url 承担 /v1 语义（与 probes /healthz 同理）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine
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
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶（连发 >5 req/s），显式调高隔离,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _create_deck(client: TestClient, user: dict[str, str]) -> str:
    resp = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()})
    assert resp.status_code == 201
    return str(resp.json()["deck_id"])


def _idempotency_rows(db_path: Path) -> int:
    """幂等记录行数（"仅一行 / 失败不落库"断言的直接观测）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() or 0


def test_decks_api_create_and_list(client: TestClient) -> None:
    """POST /decks 创建（201 + 全字段）→ GET /decks 列表 / GET /decks/{id} 可见。"""
    user = _user(client)
    resp = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "D"
    assert body["source"] == "MANUAL"
    assert body["card_count"] == 0
    assert body["due_count"] == 0
    assert body["mastered_card_count"] == 0
    assert body["review_count"] == 0
    assert body["mastery_ratio"] == 0.0
    assert body["version"] and body["created_at"] and body["updated_at"]
    deck_id = body["deck_id"]
    resp = client.get("/decks", headers=user)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["deck_id"] == deck_id
    resp = client.get(f"/decks/{deck_id}", headers=user)
    assert resp.status_code == 200
    assert resp.json()["name"] == "D"


def test_decks_api_get_cross_user_404(client: TestClient) -> None:
    """跨用户访问 → 404 DECK_NOT_FOUND（资源归属隔离）。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    resp = client.get(f"/decks/{deck_id}", headers=_user(client, "user2", "pass-2222"))
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_decks_api_delete(client: TestClient) -> None:
    """DELETE 204 → 再 GET 404；不同 key 重复删除 → 404。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 204
    assert client.get(f"/decks/{deck_id}", headers=user).status_code == 404
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_decks_api_delete_blocked_by_running_task(client: TestClient, tmp_path: Path) -> None:
    """删除保护：进行中任务引用该牌组 → 409 TASK_IN_PROGRESS（AppError → HTTP 映射）。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.begin() as conn:
        owner_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
        assert owner_id is not None
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, user_id, status, selected_chapters,"
                " generation_config, deck_id, generated_card_count, resumable,"
                " created_at, updated_at)"
                " VALUES (:task_id, :user_id, 'RUNNING', '[]', '{}', :deck_id,"
                " 0, 0, :now, :now)"
            ),
            {
                "task_id": str(uuid.uuid4()),
                "user_id": str(owner_id),
                "deck_id": deck_id,
                "now": "2026-08-11T00:00:00.000Z",
            },
        )
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_IN_PROGRESS"


def test_decks_api_create_requires_idempotency_key(client: TestClient) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）：缺失 → 400 VALIDATION_ERROR。"""
    resp = client.post("/decks", json={"name": "D"}, headers=_user(client))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_decks_api_idempotency_replay_and_conflict(client: TestClient, tmp_path: Path) -> None:
    """同设备同 key 同 body：单副作用 + 重放原响应；同 key 异 body：409。"""
    user = _user(client)
    key = _idem()
    headers = {**user, **key}
    resp1 = client.post("/decks", json={"name": "D"}, headers=headers)
    assert resp1.status_code == 201
    first = resp1.json()
    resp2 = client.post("/decks", json={"name": "D"}, headers=headers)
    assert resp2.status_code == 201
    assert resp2.json() == first  # 重放首次响应（同一 deck_id）
    resp3 = client.post("/decks", json={"name": "OTHER"}, headers=headers)
    assert resp3.status_code == 409
    assert resp3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 单副作用：列表只有 1 个牌组，幂等记录仅 1 行
    resp = client.get("/decks", headers=user)
    assert len(resp.json()["items"]) == 1
    assert _idempotency_rows(tmp_path / "api.db") == 1


def test_decks_api_idempotency_new_app_session_replays(client: TestClient, tmp_path: Path) -> None:
    """新 app/session（同库）：DB 持久化幂等记录跨会话生效 → 重放原响应。"""
    user = _user(client)
    key = _idem()
    headers = {**user, **key}
    first = client.post("/decks", json={"name": "D"}, headers=headers)
    assert first.status_code == 201
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.db'}", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client2:
        resp = client2.post("/decks", json={"name": "D"}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["deck_id"] == first.json()["deck_id"]  # 原响应原样重放


def test_decks_api_delete_idempotent_replay(client: TestClient, tmp_path: Path) -> None:
    """DELETE 成功后同 key 重放 → 204（重复提交安全返回，不 404）；不同 key 再删 → 404。"""
    user = _user(client)
    key = _idem()
    deck_id = _create_deck(client, user)
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **key})
    assert resp.status_code == 204
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **key})
    assert resp.status_code == 204  # 幂等重放：记录响应 status 204（body 存 {}）
    assert client.get(f"/decks/{deck_id}", headers=user).status_code == 404
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 404  # 新 key 不再重放 → 牌组已不存在
    # 幂等记录 = 创建 1 行 + DELETE 1 行：重放不新增，404（非 2xx）不落库
    assert _idempotency_rows(tmp_path / "api.db") == 2


def test_decks_api_delete_failed_retry_same_key_still_404(
    client: TestClient, tmp_path: Path
) -> None:
    """失败（404）不落幂等记录：同 (user, path, key) 重试仍 404，库中无记录。"""
    user = _user(client)
    key = _idem()
    headers = {**user, **key}
    deck_id = str(uuid.uuid4())
    for _ in range(2):
        resp = client.delete(f"/decks/{deck_id}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"
    assert _idempotency_rows(tmp_path / "api.db") == 0  # 失败不落库，无重放路径


def test_decks_api_rename_and_idempotent_replay(client: TestClient) -> None:
    """牌组改名（V6 前端已实现 UI 补齐）：200 + version 递增；同键重放返回首次结果。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    resp = client.get(f"/decks/{deck_id}", headers=user)
    old_version = resp.json()["version"]

    key = _idem()
    headers = {**user, **key}
    first = client.patch(f"/decks/{deck_id}", json={"name": "新名字"}, headers=headers)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["name"] == "新名字"
    assert body["version"] != old_version  # 缓存刷新信号

    replay = client.patch(f"/decks/{deck_id}", json={"name": "新名字"}, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == body  # 重放返回首次响应

    # 空名 → 400 VALIDATION_ERROR（契约第 7 章）
    resp = client.patch(f"/decks/{deck_id}", json={"name": ""}, headers={**user, **_idem()})
    assert resp.status_code == 400


def test_decks_api_rename_cross_user_404(client: TestClient) -> None:
    """跨用户改名 → 404（资源隔离，契约 1.1）。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    resp = client.patch(
        f"/decks/{deck_id}",
        json={"name": "x"},
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 404
