"""V2.5 账号资料与偏好端点集成（Task 3 NV-02；structure-contract 6.1/3.14/3.15；openapi /auth/me、/preferences）。

覆盖：GET/PATCH /preferences 默认值与部分更新、比例/每日目标/IANA 时区服务端校验
（INVALID_PREFERENCES / INVALID_LEARNING_TIMEZONE，不得被中间件泛化成 VALIDATION_ERROR——
Task 2 审查遗留 I-2）、PATCH /auth/me 昵称规则/12 预设头像/email 只读、跨用户隔离、
last-success-wins、幂等重试、API-key 字段不进 profile/preferences 载荷。
API 测试在迁移后 schema 上跑（alembic upgrade head，同 test_auth.py 款）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as OrmSession

from app.config import Settings
from app.main import create_app
from infra.db.models import LearningProject, Material, PdfFile

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

PRESET_AVATARS = [f"mood_{i:02d}" for i in range(1, 13)]


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "profile.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 流程用例快速连发，隔离 IP 5 req/s 总闸门
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _register(client: TestClient, username: str, email: str | None = None) -> dict[str, str]:
    """注册并返回 Bearer 头（每测试独立 DB，直接 register 即可）。"""
    email = email or f"{username}@example.com"
    r = client.post(
        "/auth/register", json={"username": username, "email": email, "password": "secret-pass-1"}
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _error_code(response: Any) -> str:
    code: Any = response.json()["error"]["code"]
    assert isinstance(code, str)
    return code


def _register_user(client: TestClient, username: str) -> tuple[dict[str, str], dict[str, Any]]:
    """注册并返回 (Bearer 头, user dict)——current_project_id 用例需真实 user_id 种子项目。"""
    headers = _register(client, username)
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    return headers, me.json()["user"]


def _seed_project(client: TestClient, user_id: str, project_id: str) -> None:
    """直接种子 learning_projects 行（Task 4 项目接口未落地前，测试经 ORM 建最小项目）。"""
    engine = cast(FastAPI, client.app).state.engine
    stamp = "2026-08-15T00:00:00.000Z"
    with OrmSession(engine) as session:
        session.add(
            PdfFile(
                file_id=f"{project_id}-file",
                user_id=user_id,
                filename="seed.pdf",
                storage_key="seed-key",
                size_bytes=1,
                status="PARSED",
                error_code=None,
                created_at=stamp,
            )
        )
        session.flush()  # 先落 pdf_files/materials（FK 依赖；unit-of-work 不排序裸 FK 插入）
        session.add(
            Material(
                material_id=f"{project_id}-file",  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=project_id,
                type="PDF",
                name="seed.pdf",
                status=None,
                size_bytes=1,
                created_at=stamp,
            )
        )
        session.add(
            LearningProject(
                project_id=project_id,
                user_id=user_id,
                name="seed project",
                chapters_confirmed_at=None,
                version="v1",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        session.commit()


# ---------- GET/PATCH /preferences 默认值与部分更新 ----------


def test_preferences_defaults_on_first_access(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.get("/preferences", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default_coverage_mode"] == "BALANCED"
    assert body["default_difficulty_ratio"] == {
        "basic": 40,
        "understanding": 40,
        "deep_question": 20,
    }
    assert body["daily_learning_goal"] == 50
    assert body["learning_timezone"] == "Asia/Shanghai"
    assert body["current_project_id"] is None
    assert body["updated_at"]  # 非空 ISO 字符串
    # API-key 字段不得进入偏好载荷（6.1）
    assert not (set(body) & {"api_key", "masked_key", "api_key_status"})


def test_preferences_partial_patch_keeps_other_fields(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch("/preferences", headers=headers | _idem(), json={"daily_learning_goal": 80})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["daily_learning_goal"] == 80
    assert body["default_coverage_mode"] == "BALANCED"
    assert body["default_difficulty_ratio"] == {
        "basic": 40,
        "understanding": 40,
        "deep_question": 20,
    }
    assert body["learning_timezone"] == "Asia/Shanghai"
    # 持久化：重新 GET 一致
    again = client.get("/preferences", headers=headers)
    assert again.status_code == 200
    assert again.json()["daily_learning_goal"] == 80


def test_preferences_patch_all_fields_then_last_success_wins(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences",
        headers=headers | _idem(),
        json={
            "default_coverage_mode": "EXTENSIVE",
            "default_difficulty_ratio": {"basic": 0, "understanding": 40, "deep_question": 60},
            "daily_learning_goal": 120,
            "learning_timezone": "America/New_York",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["default_coverage_mode"] == "EXTENSIVE"
    assert r.json()["default_difficulty_ratio"] == {
        "basic": 0,
        "understanding": 40,
        "deep_question": 60,
    }
    assert r.json()["daily_learning_goal"] == 120
    assert r.json()["learning_timezone"] == "America/New_York"
    # last-success-wins：再次部分更新，未提及字段保持上次成功值
    r2 = client.patch("/preferences", headers=headers | _idem(), json={"daily_learning_goal": 90})
    assert r2.status_code == 200, r2.text
    assert r2.json()["daily_learning_goal"] == 90
    assert r2.json()["default_coverage_mode"] == "EXTENSIVE"
    assert r2.json()["learning_timezone"] == "America/New_York"
    assert r2.json()["default_difficulty_ratio"]["basic"] == 0


def test_preferences_cross_user_isolation(client: TestClient) -> None:
    headers_a = _register(client, "alice")
    headers_b = _register(client, "bob")
    r = client.patch("/preferences", headers=headers_a | _idem(), json={"daily_learning_goal": 180})
    assert r.status_code == 200, r.text
    # B 仍为默认值（跨用户隔离）
    rb = client.get("/preferences", headers=headers_b)
    assert rb.status_code == 200
    assert rb.json()["daily_learning_goal"] == 50
    # A 的 PATCH 对 B 幂等键/路径无影响（同 path 不同 user 域）
    rb2 = client.patch(
        "/preferences", headers=headers_b | _idem(), json={"daily_learning_goal": 60}
    )
    assert rb2.status_code == 200, rb2.text
    assert client.get("/preferences", headers=headers_a).json()["daily_learning_goal"] == 180


# ---------- current_project_id：保持/清空/存在性（R1 审查 I-1/I-2） ----------


def test_preferences_patch_preserves_current_project_id_on_partial_update(
    client: TestClient,
) -> None:
    """R1 I-1 回归：设置 current_project_id 后再部分更新其它字段，不得静默清空。"""
    headers, user = _register_user(client, "alice")
    project_id = str(uuid.uuid4())
    _seed_project(client, user["user_id"], project_id)
    r = client.patch(
        "/preferences", headers=headers | _idem(), json={"current_project_id": project_id}
    )
    assert r.status_code == 200, r.text
    assert r.json()["current_project_id"] == project_id
    # 部分更新只动提及字段：current_project_id 保持（last-success-wins）
    r2 = client.patch("/preferences", headers=headers | _idem(), json={"daily_learning_goal": 80})
    assert r2.status_code == 200, r2.text
    assert r2.json()["current_project_id"] == project_id
    assert r2.json()["daily_learning_goal"] == 80


def test_preferences_patch_current_project_explicit_null_clears(client: TestClient) -> None:
    headers, user = _register_user(client, "alice")
    project_id = str(uuid.uuid4())
    _seed_project(client, user["user_id"], project_id)
    assert (
        client.patch(
            "/preferences", headers=headers | _idem(), json={"current_project_id": project_id}
        ).status_code
        == 200
    )
    r = client.patch("/preferences", headers=headers | _idem(), json={"current_project_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["current_project_id"] is None


def test_preferences_patch_nonexistent_project_404(client: TestClient) -> None:
    """R1 I-2 回归：格式合法但不存在的项目 UUID → 404 PROJECT_NOT_FOUND（不得 500）。"""
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences",
        headers=headers | _idem(),
        json={"current_project_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
    assert _error_code(r) == "PROJECT_NOT_FOUND"
    # 失败不落库
    assert client.get("/preferences", headers=headers).json()["current_project_id"] is None


def test_preferences_patch_other_users_project_404(client: TestClient) -> None:
    """跨用户项目 → 统一 404 PROJECT_NOT_FOUND（不暴露存在性）。"""
    headers_a = _register(client, "alice")
    _headers_b, user_b = _register_user(client, "bob")
    project_id = str(uuid.uuid4())
    _seed_project(client, user_b["user_id"], project_id)
    r = client.patch(
        "/preferences", headers=headers_a | _idem(), json={"current_project_id": project_id}
    )
    assert r.status_code == 404
    assert _error_code(r) == "PROJECT_NOT_FOUND"
    assert client.get("/preferences", headers=headers_a).json()["current_project_id"] is None


def test_preferences_patch_empty_body_true_noop(client: TestClient) -> None:
    """R1 M-2：空 PATCH 为真 no-op——不刷新 updated_at、不改任何字段。"""
    headers = _register(client, "alice")
    before = client.get("/preferences", headers=headers).json()
    r = client.patch("/preferences", headers=headers | _idem(), json={})
    assert r.status_code == 200, r.text
    after = client.get("/preferences", headers=headers).json()
    assert after == before  # 含 updated_at 逐字节一致


# ---------- 比例校验（I-2：INVALID_PREFERENCES 可达，非泛化 VALIDATION_ERROR） ----------


@pytest.mark.parametrize(
    "ratio",
    [
        {"basic": 45, "understanding": 40, "deep_question": 15},  # 非 10% 档
        {"basic": 100, "understanding": 100, "deep_question": -100},  # 越界
        {"basic": 35, "understanding": 35, "deep_question": 30},  # 合计 100 但 35 非 10% 档
    ],
)
def test_preferences_patch_ratio_step_violation_invalid_preferences(
    client: TestClient, ratio: dict[str, int]
) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences", headers=headers | _idem(), json={"default_difficulty_ratio": ratio}
    )
    assert r.status_code == 400
    assert _error_code(r) == "INVALID_PREFERENCES"  # I-2：不得被泛化成 VALIDATION_ERROR


def test_preferences_patch_ratio_sum_not_100_invalid_preferences(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences",
        headers=headers | _idem(),
        json={"default_difficulty_ratio": {"basic": 30, "understanding": 30, "deep_question": 30}},
    )
    assert r.status_code == 400
    assert _error_code(r) == "INVALID_PREFERENCES"
    # 失败不落库：默认值保持
    assert client.get("/preferences", headers=headers).json()["default_difficulty_ratio"] == {
        "basic": 40,
        "understanding": 40,
        "deep_question": 20,
    }


def test_preferences_patch_ratio_all_zero_invalid_preferences(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences",
        headers=headers | _idem(),
        json={"default_difficulty_ratio": {"basic": 0, "understanding": 0, "deep_question": 0}},
    )
    assert r.status_code == 400
    assert _error_code(r) == "INVALID_PREFERENCES"


def test_preferences_patch_ratio_single_zero_allowed(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences",
        headers=headers | _idem(),
        json={"default_difficulty_ratio": {"basic": 0, "understanding": 60, "deep_question": 40}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["default_difficulty_ratio"]["basic"] == 0


# ---------- 每日目标校验 ----------


@pytest.mark.parametrize("goal", [5, 205, 105])
def test_preferences_patch_daily_goal_invalid_preferences(client: TestClient, goal: int) -> None:
    headers = _register(client, "alice")
    r = client.patch("/preferences", headers=headers | _idem(), json={"daily_learning_goal": goal})
    assert r.status_code == 400
    assert _error_code(r) == "INVALID_PREFERENCES"


@pytest.mark.parametrize("goal", [10, 60, 200])
def test_preferences_patch_daily_goal_valid_boundaries(client: TestClient, goal: int) -> None:
    headers = _register(client, "alice")
    r = client.patch("/preferences", headers=headers | _idem(), json={"daily_learning_goal": goal})
    assert r.status_code == 200, r.text
    assert r.json()["daily_learning_goal"] == goal


# ---------- 学习时区校验 ----------


def test_preferences_patch_invalid_timezone_invalid_learning_timezone(
    client: TestClient,
) -> None:
    headers = _register(client, "alice")
    r = client.patch(
        "/preferences", headers=headers | _idem(), json={"learning_timezone": "Not/AZone"}
    )
    assert r.status_code == 400
    assert _error_code(r) == "INVALID_LEARNING_TIMEZONE"
    # 失败不落库
    assert client.get("/preferences", headers=headers).json()["learning_timezone"] == (
        "Asia/Shanghai"
    )


@pytest.mark.parametrize("tz", ["Asia/Shanghai", "UTC", "America/New_York"])
def test_preferences_patch_valid_timezones(client: TestClient, tz: str) -> None:
    headers = _register(client, "alice")
    r = client.patch("/preferences", headers=headers | _idem(), json={"learning_timezone": tz})
    assert r.status_code == 200, r.text
    assert r.json()["learning_timezone"] == tz


# ---------- /preferences 鉴权与幂等 ----------


def test_preferences_requires_auth(client: TestClient) -> None:
    assert client.get("/preferences").status_code == 401
    assert client.patch("/preferences", json={}).status_code == 401


def test_preferences_patch_requires_idempotency_key(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch("/preferences", headers=headers, json={"daily_learning_goal": 60})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"


def test_preferences_patch_idempotent_replay(client: TestClient) -> None:
    headers = _register(client, "alice")
    key = _idem()
    body = {"daily_learning_goal": 70, "learning_timezone": "Asia/Tokyo"}
    r1 = client.patch("/preferences", headers=headers | key, json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.patch("/preferences", headers=headers | key, json=body)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()  # 同键同 body 重放首次成功响应
    # 同键异 body → 409 IDEMPOTENCY_CONFLICT
    r3 = client.patch("/preferences", headers=headers | key, json={"daily_learning_goal": 80})
    assert r3.status_code == 409
    assert _error_code(r3) == "IDEMPOTENCY_CONFLICT"


# ---------- GET/PATCH /auth/me ----------


def test_me_defaults_and_no_api_key_fields(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert set(user) == {"user_id", "username", "email", "avatar_key", "created_at"}
    assert user["username"] == "alice"
    assert user["email"] == "alice@example.com"
    assert user["avatar_key"] == "mood_01"  # 默认预设头像
    # API-key 字段不得进入 profile 载荷（6.1）
    assert not (set(user) & {"api_key", "masked_key", "api_key_status"})


def test_me_patch_username_trim_and_echo(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers | _idem(), json={"username": "  bob_01  "})
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["username"] == "bob_01"  # 去首尾空白
    assert user["email"] == "alice@example.com"  # 只读
    assert user["avatar_key"] == "mood_01"
    # 持久化
    assert client.get("/auth/me", headers=headers).json()["user"]["username"] == "bob_01"


@pytest.mark.parametrize(
    "username",
    ["", "   ", "a" * 25, "bob@x!", "bad name/with-slash", "小" * 25],
)
def test_me_patch_username_invalid_400(client: TestClient, username: str) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers | _idem(), json={"username": username})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"
    # 失败不改名
    assert client.get("/auth/me", headers=headers).json()["user"]["username"] == "alice"


@pytest.mark.parametrize("avatar", PRESET_AVATARS)
def test_me_patch_avatar_accepts_all_12_presets(client: TestClient, avatar: str) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers | _idem(), json={"avatar_key": avatar})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["avatar_key"] == avatar
    assert client.get("/auth/me", headers=headers).json()["user"]["avatar_key"] == avatar


@pytest.mark.parametrize("avatar", ["mood_13", "mood_00", "avatar", ""])
def test_me_patch_avatar_invalid_400(client: TestClient, avatar: str) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers | _idem(), json={"avatar_key": avatar})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"
    assert client.get("/auth/me", headers=headers).json()["user"]["avatar_key"] == "mood_01"


def test_me_patch_email_readonly(client: TestClient) -> None:
    headers = _register(client, "alice")
    # 仅 email 的请求体：email 非可 patch 字段 → 至少一个字段规则 → 400，且邮箱不变
    r = client.patch("/auth/me", headers=headers | _idem(), json={"email": "hacked@example.com"})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"
    # 混入 email 的合法更新：email 被忽略，昵称生效
    r2 = client.patch(
        "/auth/me",
        headers=headers | _idem(),
        json={"username": "bob", "email": "hacked@example.com"},
    )
    assert r2.status_code == 200, r2.text
    user = r2.json()["user"]
    assert user["username"] == "bob"
    assert user["email"] == "alice@example.com"
    # 登录仍用原邮箱（email 未被改写）
    r3 = client.post(
        "/auth/login",
        json={"email": "alice@example.com", "password": "secret-pass-1"},
    )
    assert r3.status_code == 200, r3.text


def test_me_patch_empty_body_400(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers | _idem(), json={})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"


def test_me_patch_requires_idempotency_key(client: TestClient) -> None:
    headers = _register(client, "alice")
    r = client.patch("/auth/me", headers=headers, json={"username": "bob"})
    assert r.status_code == 400
    assert _error_code(r) == "VALIDATION_ERROR"


def test_me_patch_idempotent_replay(client: TestClient) -> None:
    headers = _register(client, "alice")
    key = _idem()
    body = {"username": "bob", "avatar_key": "mood_03"}
    r1 = client.patch("/auth/me", headers=headers | key, json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.patch("/auth/me", headers=headers | key, json=body)
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    r3 = client.patch("/auth/me", headers=headers | key, json={"username": "carol"})
    assert r3.status_code == 409
    assert _error_code(r3) == "IDEMPOTENCY_CONFLICT"


def test_me_patch_avatar_then_preferences_isolated(client: TestClient) -> None:
    """头像更新不触碰偏好，偏好更新不触碰资料（两资源独立持久化）。"""
    headers = _register(client, "alice")
    assert (
        client.patch(
            "/auth/me", headers=headers | _idem(), json={"avatar_key": "mood_05"}
        ).status_code
        == 200
    )
    prefs = client.get("/preferences", headers=headers).json()
    assert prefs["daily_learning_goal"] == 50
    assert (
        client.patch(
            "/preferences", headers=headers | _idem(), json={"daily_learning_goal": 60}
        ).status_code
        == 200
    )
    me = client.get("/auth/me", headers=headers).json()["user"]
    assert me["avatar_key"] == "mood_05"
