"""账号端点集成（DESIGN §4.4；/auth/* 契约 structure-contract §6.11）。

API 测试在迁移后 schema 上跑：client fixture 内 alembic upgrade head 建真实表结构
（与 test_decks_api.py 同款；/auth/register 需 users/auth_sessions 表）。
V2.4：登录键为 email（服务端转小写）；username 降为展示名（1-24 位中文/字母/数字/._-，
可重名，不再强制小写）。
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
    client: TestClient,
    username: str = "alice",
    email: str = "alice@example.com",
    password: str = "secret-pass-1",
) -> dict[str, str]:
    r = client.post(
        "/auth/register", json={"username": username, "email": email, "password": password}
    )
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
    r2 = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "secret-pass-1"}
    )
    assert r2.status_code == 200
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}"}
    assert client.get("/auth/me", headers=h2).status_code == 200


def test_register_email_conflict_409(client: TestClient) -> None:
    _auth_headers(client, username="bob", email="bob@example.com", password="pass-1234")
    r = client.post(
        "/auth/register",
        json={"username": "bob2", "email": "BOB@EXAMPLE.COM", "password": "pass-1234"},
    )  # 转小写后 email 冲突
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "EMAIL_TAKEN"
    # 同 username 不同 email 允许（展示名可重名）
    r2 = client.post(
        "/auth/register",
        json={"username": "bob", "email": "other@example.com", "password": "pass-1234"},
    )
    assert r2.status_code == 201


def test_login_failure_unified_401_invalid_credentials(client: TestClient) -> None:
    _auth_headers(client, username="carol", email="carol@example.com", password="pass-1234")
    r = client.post(
        "/auth/login", json={"email": "carol@example.com", "password": "wrong-password"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert r.json()["error"]["message"] == "邮箱或密码错误"
    assert "WWW-Authenticate" not in r.headers  # INVALID_CREDENTIALS 不带该头（DESIGN §4.3）
    # 邮箱不存在同样 401 同码（不暴露存在性）
    r2 = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever-1"})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_username_email_validation(client: TestClient) -> None:
    # 用户名非法例（V2.4：1-24 位中文/字母/数字/._-）
    for bad in ("", "a" * 25, "has space", "😀"):
        r = client.post(
            "/auth/register",
            json={"username": bad, "email": "bad@example.com", "password": "pass-1234"},
        )
        assert r.status_code == 400, bad
    # 合法例（新语义）：中文展示名、大写不再强制小写——注册成功且 me 返回原样
    for good, email in (("中文名", "zhongwen@example.com"), ("Tom", "tom@example.com")):
        r = client.post(
            "/auth/register", json={"username": good, "email": email, "password": "pass-1234"}
        )
        assert r.status_code == 201, good
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        me = client.get("/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["user"]["username"] == good
    # email 非法例
    for bad_email in ("no-at", "a@b c", "x" * 255):
        r = client.post(
            "/auth/register",
            json={"username": "validname", "email": bad_email, "password": "pass-1234"},
        )
        assert r.status_code == 400, bad_email
    for bad_pw in ("short7!", "x" * 129):
        r = client.post(
            "/auth/register",
            json={"username": "validname", "email": "valid@example.com", "password": bad_pw},
        )
        assert r.status_code == 400


def test_login_success_200_shape(client: TestClient) -> None:
    """login 成功 200 与 register 201 共用 AuthSessionResponse 形状（3.15）。"""
    _auth_headers(client)
    r = client.post("/auth/login", json={"email": "alice@example.com", "password": "secret-pass-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert set(body) == {"user", "access_token", "token_type", "expires_at"}
    assert set(body["user"]) == {"user_id", "username", "created_at"}
    assert body["user"]["username"] == "alice"


def test_multiple_sessions_coexist_logout_only_current(client: TestClient) -> None:
    """多会话并存；logout 只撤销当前 session（DESIGN §4.3）。"""
    headers1 = _auth_headers(client)
    r2 = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "secret-pass-1"}
    )
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
        user_id = conn.execute(
            text("SELECT user_id FROM users WHERE email = 'alice@example.com'")
        ).scalar()
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
    r = client.post(
        "/auth/register",
        json={"username": "dave", "email": "dave@example.com", "password": "pass-1234"},
    )
    assert r.status_code == 201
    r = client.post(
        "/auth/login",
        json={"email": "dave@example.com", "password": "pass-1234"},
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


def test_sliding_renewal_extends_near_expiry_session(client: TestClient, tmp_path: Path) -> None:
    """滑动续期：剩余 <1 天的会话经任一受保护请求后延长至 ~now+30 天。"""
    headers = _auth_headers(client, email="rene@example.com")
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    # 把会话拨到「剩余 12 小时」：expires_at = now + 0.5d
    # 不变量：expires_at 全库必须保持 format_utc 同构的 T 格式——resolve_principal
    # 依赖字符串比较，space 格式（datetime() 输出）在同日运行时会误判过期。
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE auth_sessions SET expires_at = "
                "strftime('%Y-%m-%dT%H:%M:%S.000Z','now','+12 hours')"
            )
        )
    assert client.get("/auth/me", headers=headers).status_code == 200
    # format_utc 输出为 ISO Z 后缀；比较用 SQL：剩余应 > 29 天（已续到 ~30 天）
    with engine.connect() as conn:
        remaining_days = conn.execute(
            text("SELECT (julianday(expires_at) - julianday('now')) FROM auth_sessions")
        ).scalar()
    assert remaining_days is not None
    assert remaining_days > 29.0


def test_fresh_session_not_renewed(client: TestClient, tmp_path: Path) -> None:
    """节流：剩余 >1 天的会话不触发续期写库（expires_at 原值不动）。"""
    headers = _auth_headers(client, email="fresh@example.com")
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    assert client.get("/auth/me", headers=headers).status_code == 200
    with engine.connect() as conn:
        expires_before = conn.execute(text("SELECT expires_at FROM auth_sessions")).scalar()
    assert client.get("/auth/me", headers=headers).status_code == 200
    with engine.connect() as conn:
        expires_after = conn.execute(text("SELECT expires_at FROM auth_sessions")).scalar()
    assert expires_before == expires_after


def test_revoked_session_never_renewed(client: TestClient, tmp_path: Path) -> None:
    """已撤销会话经 resolve 挡回 401，不触发续期。"""
    headers = _auth_headers(client, email="revoked@example.com")
    # logout 为写接口须携带 Idempotency-Key（contract 6.11，同 test_logout_missing_idempotency_key_400）
    assert (
        client.post(
            "/auth/logout",
            headers={**headers, "Idempotency-Key": "66666666-6666-4666-8666-666666666666"},
        ).status_code
        == 204
    )
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as conn:
        revoked_at = conn.execute(text("SELECT revoked_at FROM auth_sessions")).scalar()
    assert revoked_at is not None
    assert client.get("/auth/me", headers=headers).status_code == 401
