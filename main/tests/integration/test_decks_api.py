"""牌组 API 集成测试（HTTP 层 + 幂等接线 + 跨设备 404 + 删除保护）。

API 测试在迁移后 schema 上跑：client fixture 内 alembic upgrade head 建真实表结构。
路径无 /v1 前缀——openapi servers url 承担 /v1 语义（与 probes /healthz 同理）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text

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


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    engine.dispose()
    assert row is not None
    return str(row)


def _seed_project(db_path: Path, *, user_id: str, with_key: bool = False) -> dict[str, object]:
    """ORM 直种 PARSED PDF + 2 章节 + 学习项目（+ 可选 ApiKey）。

    V2.5 前置：任务创建要求项目归属牌组与已保存 Key（6.2），牌组由被测 API 路径创建；
    项目/PDF/章节种子沿用 test_tasks_api._seed_context 同款模式（本测试只测
    deck.project_id 写路径，不重复覆盖项目域）。
    """
    from infra.db.models import ApiKey, Chapter, LearningProject, PdfFile, User
    from infra.db.session import create_db_engine, create_session_factory

    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    file_id, project_id = str(uuid.uuid4()), str(uuid.uuid4())
    chapter_ids: list[str] = []
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用
            session.add(
                User(
                    user_id=user_id,
                    username=f"u-{user_id[:8]}",
                    email=f"u-{user_id[:8]}@example.com",
                    password_hash="x",
                    created_at="2026-08-11T00:00:00.000Z",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
        session.add(
            PdfFile(
                file_id=file_id,
                user_id=user_id,
                filename="seed.pdf",
                storage_key=str(uuid.uuid4()),
                size_bytes=10,
                status="PARSED",
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        session.add(
            LearningProject(
                project_id=project_id,
                user_id=user_id,
                file_id=file_id,
                name="P",
                chapters_confirmed_at="2026-08-11T00:00:00.000Z",
                version="2026-08-11T00:00:00.000Z",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        for i in range(2):
            cid = str(uuid.uuid4())
            session.add(
                Chapter(
                    chapter_id=cid,
                    file_id=file_id,
                    name=f"第{i + 1}章",
                    start_page=i + 1,
                    end_page=i + 2,
                )
            )
            session.flush()
            chapter_ids.append(cid)
        if with_key:
            # 只校验 status=AVAILABLE 行存在（_require_api_key 不解密），占位密文即可
            session.execute(
                insert(ApiKey).values(
                    user_id=user_id,
                    encrypted_key="enc",
                    status="AVAILABLE",
                    masked_key="sk-****",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
        session.commit()
    return {"project_id": project_id, "file_id": file_id, "chapter_ids": chapter_ids}


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


def test_decks_api_delete_auto_cancels_running_task(client: TestClient, tmp_path: Path) -> None:
    """契约 722：删除牌组自动取消进行中任务并删除卡片与学习记录，任务历史保留。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    task_id = str(uuid.uuid4())
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.begin() as conn:
        owner_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
        assert owner_id is not None
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, user_id, status, selected_chapters,"
                " generation_config, deck_id, generated_card_count, resumable,"
                " created_at, updated_at)"
                " VALUES (:task_id, :user_id, 'GENERATING', '[]', '{}', :deck_id,"
                " 0, 0, :now, :now)"
            ),
            {
                "task_id": task_id,
                "user_id": str(owner_id),
                "deck_id": deck_id,
                "now": "2026-08-11T00:00:00.000Z",
            },
        )
    engine.dispose()
    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 204, resp.text
    assert client.get(f"/decks/{deck_id}", headers=user).status_code == 404
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, deck_id, ended_at FROM tasks WHERE task_id = :task_id"),
            {"task_id": task_id},
        ).one()
    engine.dispose()
    assert row.status == "ABANDONED"
    assert row.deck_id is None
    assert row.ended_at is not None


def test_decks_api_deletion_preflight_lists_task_actions(
    client: TestClient, tmp_path: Path
) -> None:
    """牌组预检返回影响范围、任务状态和可执行动作。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    task_id = str(uuid.uuid4())
    with engine.begin() as conn:
        owner_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
        assert owner_id is not None
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, user_id, status, selected_chapters,"
                " generation_config, deck_id, generated_card_count, resumable,"
                " created_at, updated_at)"
                " VALUES (:task_id, :user_id, 'DRAFT', '[]', '{}', :deck_id,"
                " 0, 0, :now, :now)"
            ),
            {
                "task_id": task_id,
                "user_id": str(owner_id),
                "deck_id": deck_id,
                "now": "2026-08-11T00:00:00.000Z",
            },
        )
    engine.dispose()

    resp = client.get(f"/decks/{deck_id}/deletion-preflight", headers=user)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resource_type"] == "DECK"
    assert body["resource_id"] == deck_id
    assert body["can_delete"] is False
    assert body["abandonable_task_ids"] == [task_id]
    assert body["has_uncancellable_tasks"] is False
    assert body["actions"] == ["ABANDON_AND_RETRY"]
    assert body["impact"]["deck_name"] == "D"
    assert body["impact"]["task_count"] == 1


def test_decks_api_delete_auto_cancels_pre_generation_task_and_keeps_history(
    client: TestClient, tmp_path: Path
) -> None:
    """删除牌组自动取消正式生成前任务，任务历史保留且脱离牌组。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    task_id = str(uuid.uuid4())
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.begin() as conn:
        owner_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
        assert owner_id is not None
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, user_id, status, selected_chapters,"
                " generation_config, deck_id, generated_card_count, resumable,"
                " created_at, updated_at)"
                " VALUES (:task_id, :user_id, 'SAMPLE_GENERATING', '[]', '{}', :deck_id,"
                " 0, 0, :now, :now)"
            ),
            {
                "task_id": task_id,
                "user_id": str(owner_id),
                "deck_id": deck_id,
                "now": "2026-08-11T00:00:00.000Z",
            },
        )
    engine.dispose()

    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 204, resp.text
    assert client.get(f"/decks/{deck_id}", headers=user).status_code == 404
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, deck_id, ended_at, resumable FROM tasks WHERE task_id = :task_id"),
            {"task_id": task_id},
        ).one()
    engine.dispose()
    assert row.status == "ABANDONED"
    assert row.deck_id is None
    assert row.ended_at is not None
    assert row.resumable == 0


def test_decks_api_delete_cancels_generating_task_with_fencing(
    client: TestClient, tmp_path: Path
) -> None:
    """契约 570：GENERATING 不再阻塞删除——任务 CAS 取消、围栏（lease 失效）并脱离牌组。"""
    user = _user(client)
    deck_id = _create_deck(client, user)
    task_id = str(uuid.uuid4())
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.begin() as conn:
        owner_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
        assert owner_id is not None
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, user_id, status, selected_chapters,"
                " generation_config, deck_id, generated_card_count, resumable,"
                " created_at, updated_at)"
                " VALUES (:task_id, :user_id, 'GENERATING', '[]', '{}', :deck_id,"
                " 0, 0, :now, :now)"
            ),
            {
                "task_id": task_id,
                "user_id": str(owner_id),
                "deck_id": deck_id,
                "now": "2026-08-11T00:00:00.000Z",
            },
        )
    engine.dispose()

    resp = client.delete(f"/decks/{deck_id}", headers={**user, **_idem()})
    assert resp.status_code == 204, resp.text
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT status, deck_id, ended_at, resumable FROM tasks WHERE task_id = :task_id"),
            {"task_id": task_id},
        ).one()
    engine.dispose()
    assert row.status == "ABANDONED"
    assert row.deck_id is None
    assert row.ended_at is not None
    assert row.resumable == 0


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


# ---------- V2.5：deck.project_id 生产写路径（OPEN-1 裁决；结构契约 3.8） ----------


def test_decks_api_create_with_project_id_persists(client: TestClient, tmp_path: Path) -> None:
    """POST /decks 带 project_id → 201 响应含 project_id 且落库（归属学习项目）。

    RED 证据：修复前 handler 静默丢弃 payload.project_id——响应缺 project_id、
    列表过滤失效（OPEN-1 裁决：deck.project_id 无生产写路径）。
    """
    user = _user(client)
    seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db"))
    project_id = str(seed["project_id"])
    resp = client.post(
        "/decks", json={"name": "PD", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["project_id"] == project_id
    deck_id = body["deck_id"]
    # 落库断言：直接观测 decks 行
    engine = create_db_engine(f"sqlite:///{tmp_path / 'api.db'}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT project_id FROM decks WHERE deck_id = :d"), {"d": deck_id}
        ).scalar()
    assert row == project_id
    # 详情与列表均透出 project_id
    assert client.get(f"/decks/{deck_id}", headers=user).json()["project_id"] == project_id
    items = client.get("/decks", headers=user).json()["items"]
    assert [it["deck_id"] for it in items] == [deck_id]
    assert items[0]["project_id"] == project_id


def test_decks_api_create_with_project_id_cross_user_404(
    client: TestClient, tmp_path: Path
) -> None:
    """跨用户项目归属 → 404 PROJECT_NOT_FOUND（统一 404 不暴露存在性，6.2 同口径）。"""
    user = _user(client)
    other = _user(client, "user2", "pass-2222")
    seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db", "user2"))
    resp = client.post(
        "/decks",
        json={"name": "PD", "project_id": str(seed["project_id"])},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    # 归属校验失败不落幂等记录（非 2xx 失败不落库，与 DELETE 404 同语义）
    assert _idempotency_rows(tmp_path / "api.db") == 0
    # 列表互不泄漏：alice 无牌组，user2 也无
    assert client.get("/decks", headers=user).json()["items"] == []
    assert client.get("/decks", headers=other).json()["items"] == []


def test_decks_api_create_with_project_id_missing_404(client: TestClient) -> None:
    """不存在项目 → 404 PROJECT_NOT_FOUND（不做静默 null 回落）。"""
    user = _user(client)
    resp = client.post(
        "/decks",
        json={"name": "PD", "project_id": str(uuid.uuid4())},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_decks_api_list_filter_by_project_id(client: TestClient, tmp_path: Path) -> None:
    """GET /decks?project_id=X 过滤（openapi listDecks）；跨用户过滤 → 404。"""
    user = _user(client)
    seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db"))
    project_id = str(seed["project_id"])
    _create_deck(client, user)  # 独立牌组 project_id=null
    resp = client.post(
        "/decks", json={"name": "PD", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert resp.status_code == 201
    project_deck_id = resp.json()["deck_id"]
    # 全部可见（2 个）；按项目过滤只剩归属牌组
    assert len(client.get("/decks", headers=user).json()["items"]) == 2
    items = client.get(f"/decks?project_id={project_id}", headers=user).json()["items"]
    assert [it["deck_id"] for it in items] == [project_deck_id]
    assert all(it["project_id"] == project_id for it in items)
    # 无牌组项目 → 空列表（200，非 404）
    empty_seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db"))
    items = client.get(f"/decks?project_id={empty_seed['project_id']}", headers=user).json()[
        "items"
    ]
    assert items == []
    # 跨用户项目过滤 → 404（与 tasks 列表 project 过滤同口径）
    _user(client, "user2", "pass-2222")  # 先注册 user2（用户行查询依赖）
    other_seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db", "user2"))
    resp = client.get(f"/decks?project_id={other_seed['project_id']}", headers=user)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_decks_api_project_deck_enables_task_creation(client: TestClient, tmp_path: Path) -> None:
    """端到端：建项目 → API 建 deck 带 project_id → POST /projects/{id}/tasks 成功。

    RED 证据：修复前 API 建牌组 project_id 静默置空 → _require_same_project_deck
    判 deck.project_id != project_id → 404 DECK_NOT_FOUND，生成主链经公开 API 不可达
    （NV-06 今日计划对真实用户恒空）。
    """
    user = _user(client)
    seed = _seed_project(tmp_path / "api.db", user_id=_user_id(tmp_path / "api.db"), with_key=True)
    project_id = str(seed["project_id"])
    resp = client.post(
        "/decks", json={"name": "PD", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert resp.status_code == 201
    deck_id = resp.json()["deck_id"]
    resp = client.post(
        f"/projects/{project_id}/tasks",
        json={
            "deck_id": deck_id,
            "chapter_ids": seed["chapter_ids"],
            "generation_config": {
                "coverage_mode": "COMPACT",
                "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
            },
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["project_id"] == project_id
    assert body["deck_id"] == deck_id
