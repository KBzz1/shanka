"""账号端点集成（DESIGN §4.4；/auth/* 契约 structure-contract §6.11）。

API 测试在迁移后 schema 上跑：client fixture 内 alembic upgrade head 建真实表结构
（与 test_decks_api.py 同款；/auth/register 需 users/auth_sessions 表）。
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
from services.auth.tokens import hash_session_token

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "auth.db"
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


def _auth_headers(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert set(body) == {"user", "access_token", "token_type", "expires_at"}
    assert set(body["user"]) == {"user_id", "username", "created_at"}
    assert body["user"]["username"] == username
    return {"Authorization": f"Bearer {body['access_token']}"}


def test_register_login_logout_me_flow(client: TestClient) -> None:
    headers = _auth_headers(client)
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json() == {
        "user": {
            "user_id": me.json()["user"]["user_id"],
            "username": "alice",
            "created_at": me.json()["user"]["created_at"],
        }
    }
    # logout 只撤销当前 session
    assert (
        client.post(
            "/auth/logout",
            headers={**headers, "Idempotency-Key": "22222222-2222-4222-8222-222222222222"},
        ).status_code
        == 204
    )
    assert client.get("/auth/me", headers=headers).status_code == 401
    # 重新登录生成新 session
    r2 = client.post("/auth/login", json={"username": "alice", "password": "secret-pass-1"})
    assert r2.status_code == 200
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get("/auth/me", headers=h2).status_code == 200


def test_register_username_conflict_409(client: TestClient) -> None:
    _auth_headers(client, username="bob", password="pass-1234")
    r = client.post(
        "/auth/register", json={"username": "BOB", "password": "pass-1234"}
    )  # 转小写后冲突
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "USERNAME_TAKEN"


def test_login_failure_unified_401_invalid_credentials(client: TestClient) -> None:
    _auth_headers(client, username="carol", password="pass-1234")
    r = client.post("/auth/login", json={"username": "carol", "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert "WWW-Authenticate" not in r.headers  # INVALID_CREDENTIALS 不带该头（DESIGN §4.3）
    # 用户名不存在同样 401 同码（不暴露存在性）
    r2 = client.post("/auth/login", json={"username": "nobody", "password": "whatever-1"})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_username_validation_and_normalization(client: TestClient) -> None:
    for bad in ("ab", "a" * 33, "has space", "UPPER@X", "中文名"):
        r = client.post("/auth/register", json={"username": bad, "password": "pass-1234"})
        assert r.status_code == 400, bad
    for bad_pw in ("short7!", "x" * 129):
        r = client.post("/auth/register", json={"username": "validname", "password": bad_pw})
        assert r.status_code == 400


def test_login_success_200_shape(client: TestClient) -> None:
    """login 成功 200 与 register 201 共用 AuthSessionResponse 形状（3.15）。"""
    _auth_headers(client)
    r = client.post("/auth/login", json={"username": "alice", "password": "secret-pass-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert set(body) == {"user", "access_token", "token_type", "expires_at"}
    assert set(body["user"]) == {"user_id", "username", "created_at"}
    assert body["user"]["username"] == "alice"


def test_multiple_sessions_coexist_logout_only_current(client: TestClient) -> None:
    """多会话并存；logout 只撤销当前 session（DESIGN §4.3）。"""
    headers1 = _auth_headers(client)
    r2 = client.post("/auth/login", json={"username": "alice", "password": "secret-pass-1"})
    assert r2.status_code == 200
    headers2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get("/auth/me", headers=headers1).status_code == 200
    assert client.get("/auth/me", headers=headers2).status_code == 200
    resp = client.post(
        "/auth/logout",
        headers={**headers1, "Idempotency-Key": "44444444-4444-4444-8444-444444444444"},
    )
    assert resp.status_code == 204
    assert client.get("/auth/me", headers=headers1).status_code == 401
    assert client.get("/auth/me", headers=headers2).status_code == 200


def test_expired_session_401_auth_invalid(client: TestClient, tmp_path: Path) -> None:
    """过期 session → 401 AUTH_INVALID（中间件 expires_at 判定）。"""
    _auth_headers(client)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as conn:
        user_id = conn.execute(text("SELECT user_id FROM users WHERE username = 'alice'")).scalar()
    token = "expired-session-token-value"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO auth_sessions"
                " (session_id, user_id, token_hash, created_at, expires_at, revoked_at)"
                " VALUES (:sid, :uid, :th, :now, :exp, NULL)"
            ),
            {
                "sid": str(uuid.uuid4()),
                "uid": user_id,
                "th": hash_session_token(token),
                "now": "2020-01-01T00:00:00.000Z",
                "exp": "2020-01-02T00:00:00.000Z",
            },
        )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"


def test_register_login_exempt_from_idempotency_key(client: TestClient, tmp_path: Path) -> None:
    """register/login 豁免 Idempotency-Key（contract 6.11 幂等列「-」），不落幂等记录。"""
    r = client.post("/auth/register", json={"username": "dave", "password": "pass-1234"})
    assert r.status_code == 201
    r = client.post(
        "/auth/login",
        json={"username": "dave", "password": "pass-1234"},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar()
    assert count == 0


def test_logout_missing_idempotency_key_400(client: TestClient) -> None:
    """logout 缺 Idempotency-Key → 400 VALIDATION_ERROR（写接口幂等键强制，1.3/6.11）。"""
    headers = _auth_headers(client)
    r = client.post("/auth/logout", headers=headers)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_me_auth_invalid_401_carries_www_authenticate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """me 窄竞态（T2 Minor ①）：token 经中间件 resolve 后被撤销，service 抛 AUTH_INVALID
    的 401 路径——error_handler 统一补 WWW-Authenticate: Bearer。

    HTTP 层无法在 resolve 与 handler 读库之间插入撤销，monkeypatch handler 侧
    get_current_user 抛 AUTH_INVALID 模拟该竞态。
    """
    import app.api.auth as auth_api
    from app.errors import AppError, ErrorCode

    def _revoked(*args: object, **kwargs: object) -> dict[str, str]:
        raise AppError(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")

    monkeypatch.setattr(auth_api, "get_current_user", _revoked)
    headers = _auth_headers(client)
    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_logout_idempotent_single_side_effect(client: TestClient, tmp_path: Path) -> None:
    """logout 走 execute_idempotent：首次 204 + 幂等记录仅 1 行（单副作用）。

    同键顺序重放不可达：首次 logout 已撤销 session，重放请求在 Bearer 中间件即
    401 AUTH_INVALID（DESIGN §4.3 撤销 token 统一 401）——execute_idempotent
    保护的是并发双发（同一 token 同时过中间件后单副作用落库）。
    """
    headers = _auth_headers(client)
    key = "55555555-5555-4555-8555-555555555555"
    assert (
        client.post("/auth/logout", headers={**headers, "Idempotency-Key": key}).status_code == 204
    )
    replay = client.post("/auth/logout", headers={**headers, "Idempotency-Key": key})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "AUTH_INVALID"
    assert client.get("/auth/me", headers=headers).status_code == 401
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar()
    assert count == 1
