"""今日学习计划 HTTP 端点测试（structure-contract 6.6；openapi /study/plan、/study/today/backlog）。

补齐集成缺口：GET/PUT /study/plan 与 GET /study/today/backlog 此前仅有 service 级
覆盖（test_today_study_plan.py，HTTP /study/today 见 test_free_browse.py）；本文件走
真实 HTTP 栈——
- GET /study/plan 未配置空态（configured=false + 默认双目标 10/40）；
- PUT /study/plan 幂等写（Idempotency-Key 强制）：成功翻 configured、同键重放一致、
  目标校验（0~200 的 10 倍数且不同时为 0）与卡组归属校验（跨项目 → 404）；
- GET /study/today/backlog 未配置空态 + 分页参数边界（limit 1~200）。
"""

import uuid
from pathlib import Path
from typing import Any, cast

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

    db_path = tmp_path / "study_plan.db"
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
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _project(client: TestClient, user: dict[str, str], name: str = "学习项目") -> str:
    r = client.post("/projects", json={"name": name}, headers={**user, **_idem()})
    assert r.status_code == 201, r.text
    return cast(str, r.json()["project_id"])


def _deck(client: TestClient, user: dict[str, str], project_id: str, name: str = "D") -> str:
    r = client.post(
        "/decks", json={"name": name, "project_id": project_id}, headers={**user, **_idem()}
    )
    assert r.status_code == 201, r.text
    return cast(str, r.json()["deck_id"])


def _card(client: TestClient, user: dict[str, str], deck_id: str) -> str:
    """计划所选卡组须含至少一张可见卡（update_study_plan 前置校验）。"""
    r = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**user, **_idem()}
    )
    assert r.status_code == 201, r.text
    return cast(str, r.json()["card_id"])


def test_study_plan_get_unconfigured_returns_defaults(client: TestClient) -> None:
    """GET /study/plan 空态：未配置 → configured=false + 默认双目标，200 而非 404。"""
    headers = _user(client)
    r = client.get("/study/plan", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["current_project_id"] is None
    assert body["selected_deck_ids"] == []
    assert body["daily_new_goal"] == 10
    assert body["daily_review_goal"] == 40
    assert body["updated_at"] is None


def test_study_plan_put_updates_and_replays_idempotently(client: TestClient) -> None:
    """PUT /study/plan：写入后翻 configured；同键同体重放返回一致（幂等投影）。"""
    headers = _user(client)
    project_id = _project(client, headers)
    deck_id = _deck(client, headers, project_id)
    _card(client, headers, deck_id)

    payload = {
        "project_id": project_id,
        "selected_deck_ids": [deck_id],
        "daily_new_goal": 20,
        "daily_review_goal": 40,
    }
    key = _idem()
    r1 = client.put("/study/plan", json=payload, headers={**headers, **key})
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["configured"] is True
    assert body1["current_project_id"] == project_id
    assert body1["selected_deck_ids"] == [deck_id]
    assert body1["daily_new_goal"] == 20

    r2 = client.put("/study/plan", json=payload, headers={**headers, **key})
    assert r2.status_code == 200
    assert r2.json() == body1

    persisted = client.get("/study/plan", headers=headers).json()
    assert persisted["daily_new_goal"] == 20
    assert persisted["selected_deck_ids"] == [deck_id]


def test_study_plan_put_requires_idempotency_key(client: TestClient) -> None:
    """写接口强制 Idempotency-Key：缺失 → 400 VALIDATION_ERROR。"""
    headers = _user(client)
    project_id = _project(client, headers)
    r = client.put(
        "/study/plan",
        json={
            "project_id": project_id,
            "selected_deck_ids": [_deck(client, headers, project_id)],
            "daily_new_goal": 10,
            "daily_review_goal": 40,
        },
        headers=headers,  # 无 Idempotency-Key
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_study_plan_put_validates_goals_and_deck_ownership(client: TestClient) -> None:
    """目标须为 0~200 的 10 倍数且不同时为 0；所选卡组必须属于该项目（跨项目 404）。"""
    headers = _user(client)
    project_id = _project(client, headers)
    deck_id = _deck(client, headers, project_id)
    other_project = _project(client, headers, name="其他项目")
    other_deck_id = _deck(client, headers, other_project, name="其他项目牌组")

    def _put(goal_new: int, goal_review: int, decks: list[str]) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            client.put(
                "/study/plan",
                json={
                    "project_id": project_id,
                    "selected_deck_ids": decks,
                    "daily_new_goal": goal_new,
                    "daily_review_goal": goal_review,
                },
                headers={**headers, **_idem()},
            ).json(),
        )

    assert _put(15, 40, [deck_id])["error"]["code"] == "VALIDATION_ERROR"  # 非 10 倍数
    assert _put(0, 0, [deck_id])["error"]["code"] == "VALIDATION_ERROR"  # 双 0
    assert _put(10, 40, [])["error"]["code"] == "VALIDATION_ERROR"  # 空卡组
    assert _put(10, 40, [str(uuid.uuid4())])["error"]["code"] == "DECK_NOT_FOUND"
    # 归属校验：卡组属于同用户另一项目，对该项目计划而言即不可选（统一 404）
    assert _put(10, 40, [other_deck_id])["error"]["code"] == "DECK_NOT_FOUND"


def test_study_backlog_unconfigured_empty_and_pagination_bounds(client: TestClient) -> None:
    """GET /study/today/backlog：未配置 → 空集；offset/limit 越界 → 400。"""
    headers = _user(client)
    r = client.get("/study/today/backlog", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["offset"] == 0
    assert body["limit"] == 50

    assert (
        client.get("/study/today/backlog", params={"offset": -1}, headers=headers).status_code
        == 400
    )
    assert (
        client.get("/study/today/backlog", params={"limit": 0}, headers=headers).status_code == 400
    )
    assert (
        client.get("/study/today/backlog", params={"limit": 201}, headers=headers).status_code
        == 400
    )


def test_study_endpoints_require_auth(client: TestClient) -> None:
    """Bearer 强制：三个端点未认证统一 401（不暴露端点存在性差异）。"""
    for path in ("/study/plan", "/study/today/backlog"):
        assert client.get(path).status_code == 401
    assert (
        client.put(
            "/study/plan", json={}, headers={"Idempotency-Key": str(uuid.uuid4())}
        ).status_code
        == 401
    )
