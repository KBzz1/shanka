"""用户隔离（DESIGN §5.1：跨用户资源统一 404，不暴露存在性）。

P4-4 起 auth_headers 仅提供 Bearer；资源归属按 user 域——跨用户访问与他用户资源
挂接一律 404（替换原 device 隔离断言口径）。
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

    db_path = tmp_path / "iso.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _user(client: TestClient, username: str, password: str) -> dict[str, str]:
    """独立用户的 Bearer 头（P4-4 起无 X-Device-ID）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_chapter(db_path: Path, file_id: str) -> str:
    """直种章节行（upload 后 PENDING 无章节；create_task 需要 chapter_ids 非空）。"""
    from sqlalchemy import text

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    chapter_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO chapters (chapter_id, file_id, name, start_page, end_page)"
                " VALUES (:c, :f, '第1章', 1, 1)"
            ),
            {"c": chapter_id, "f": file_id},
        )
    return chapter_id


def _create_deck(client: TestClient, user: dict[str, str], *, project_id: str | None = None) -> str:
    resp = client.post(
        "/decks", json={"name": "D", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["deck_id"])


def test_cross_user_deck_404(client: TestClient) -> None:
    """user1 创建的牌组：user2 详情/删除一律 404（不暴露存在性）。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    deck_id = _create_deck(client, h1)
    resp = client.get(f"/decks/{deck_id}", headers=h2)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"
    resp = client.delete(f"/decks/{deck_id}", headers={**h2, **_idem()})
    assert resp.status_code == 404
    # user1 自身仍可见（未被 user2 删除）
    assert client.get(f"/decks/{deck_id}", headers=h1).status_code == 200


def test_card_deck_user_consistency(client: TestClient) -> None:
    """Card↔Deck 一致性守卫：user2 用 user1 的 deck_id 建卡 → 404 而非 201；
    user2 列 user1 牌组的卡 → 404。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    deck_id = _create_deck(client, h1)
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "f", "back": "b"},
        headers={**h2, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"
    assert client.get(f"/decks/{deck_id}/cards", headers=h2).status_code == 404
    # user1 建卡成功且可见
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "f", "back": "b"},
        headers={**h1, **_idem()},
    )
    assert resp.status_code == 201
    card_id = resp.json()["card_id"]
    # user2 无法编辑/删除 user1 的卡
    resp = client.patch(
        f"/cards/{card_id}", json={"front": "x", "back": "y"}, headers={**h2, **_idem()}
    )
    assert resp.status_code == 404
    resp = client.delete(f"/cards/{card_id}", headers={**h2, **_idem()})
    assert resp.status_code == 404
    assert client.get(f"/decks/{deck_id}/cards", headers=h1).status_code == 200


def test_task_project_deck_user_consistency(client: TestClient, tmp_path: Path) -> None:
    """Task↔Project/Deck 一致性守卫：user2 用 user1 的 project 建任务 → 404；
    user2 用自己的 project 但挂 user1 的 deck_id → 404。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    # 各自上传 PDF 建立学习项目（201；fake bytes 通过三重校验）
    pdf_bytes = b"%PDF-1.4 fake pdf content for upload validation"
    r1 = client.post(
        "/projects",
        files={"file": ("a.pdf", pdf_bytes, "application/pdf")},
        headers={**h1, **_idem()},
    )
    assert r1.status_code == 201, r1.text
    r2 = client.post(
        "/projects",
        files={"file": ("b.pdf", pdf_bytes, "application/pdf")},
        headers={**h2, **_idem()},
    )
    assert r2.status_code == 201, r2.text
    project1 = r1.json()["project_id"]
    project2 = r2.json()["project_id"]
    deck1 = _create_deck(client, h1, project_id=project1)
    chapter1 = _seed_chapter(tmp_path / "iso.db", r1.json()["file"]["file_id"])
    chapter2 = _seed_chapter(tmp_path / "iso.db", r2.json()["file"]["file_id"])
    payload = {
        "deck_id": deck1,
        "chapter_ids": [chapter1],
        "generation_config": {
            "coverage_mode": "COMPACT",
            "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
        },
    }
    # V2.5 4.3：任务挂项目（POST /projects/{id}/tasks）——跨用户项目 → 404 不暴露存在性
    resp = client.post(f"/projects/{project1}/tasks", json=payload, headers={**h2, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PROJECT_NOT_FOUND"
    # user2 自己的 project + user1 的 deck → 404 DECK_NOT_FOUND
    payload["chapter_ids"] = [chapter2]
    resp = client.post(f"/projects/{project2}/tasks", json=payload, headers={**h2, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_cross_user_review_404(client: TestClient) -> None:
    """复习一致性守卫：user2 取 user1 牌组队列 → 404；user2 评 user1 的卡 → 404。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    deck_id = _create_deck(client, h1)
    card = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**h1, **_idem()}
    ).json()
    resp = client.get(f"/decks/{deck_id}/review", headers=h2)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"
    resp = client.post(
        "/review-events",
        json={
            "card_id": card["card_id"],
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**h2, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"


def test_cross_user_stats_isolation(client: TestClient) -> None:
    """看板隔离：user1 有复习数据；user2 看板无数据（has_data False）。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    deck_id = _create_deck(client, h1)
    card = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**h1, **_idem()}
    ).json()
    resp = client.post(
        "/review-events",
        json={
            "card_id": card["card_id"],
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
            "device_timezone": "Asia/Shanghai",
        },
        headers={**h1, **_idem()},
    )
    assert resp.status_code == 200
    d1 = client.get("/stats/dashboard", headers=h1).json()
    assert d1["has_data"] is True
    assert d1["weekly_total"] == 1
    d2 = client.get("/stats/dashboard", headers=h2).json()
    assert d2["has_data"] is False
    assert d2["weekly_total"] == 0


def test_cross_user_rewrite_404(client: TestClient) -> None:
    """重写归属守卫：user2 重写 user1 的卡 → 404（归属校验先于 Key 解析，无需 Key）。"""
    h1 = _user(client, "user1", "pass-1111")
    h2 = _user(client, "user2", "pass-2222")
    deck_id = _create_deck(client, h1)
    card = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**h1, **_idem()}
    ).json()
    resp = client.post(f"/cards/{card['card_id']}/rewrite-previews", headers={**h2, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"
