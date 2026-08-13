# 账号后端切换 P4 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用最简用户名/密码账号体系 + opaque Bearer session 完全替代 X-Device-ID 认证，全部业务/幂等/统计/限流按 user_id 归属，X-Device-ID 退出普通运行时认证/授权/幂等/限流面。

**Architecture:** strangler 顺序（先加后删）：先新增 Bearer 认证中间件与 auth 端点（双头过渡窗口），再把业务面 ownership 参数从 device_id 切到 principal.user_id，随后删除 X-Device-ID middleware 与 devices 自动注册，最后完成 api_key 用户域写侧重映射与契约收尾。每任务结束全量测试绿。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、Alembic（SQLite）、argon2-cffi、secrets/hashlib（stdlib）。

## 全局约束

1. 执行环境：`cd /home/kbzz1/shanka_backend/main`；解释器一律 `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`；禁止把依赖装入 base/系统 Python；新依赖只进 `main/pyproject.toml`（唯一事实源），锁定文件按 pyproject 更新。
2. 四工具全绿（每任务验收）：`python -m pytest`（全部）、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`（line-length 100、mypy strict）。
3. TDD：先写失败测试并运行确认失败（红色），再实现（绿色）；每任务按提交信息 commit。
4. 红线 1：app/schemas ↔ openapi.yaml ↔ structure-contract.md 三处一致（守卫强制）；红线 2：ORM ↔ database-design.md 表结构一致；红线 3：幂等键/鉴权/错误码格式只在 app/middleware 实现。
5. 契约权威（DESIGN §4，逐字）：用户名 3～32 位 `[a-z0-9._-]` 服务端转小写；密码 8～128 字符不截断/不 Unicode 归一化/不大小写转换；Argon2id 生产参数不低于 `memory_cost=19456 KiB, time_cost=2, parallelism=1`（测试可注入低成本 hasher，生产默认与配置守卫不得被测试参数降低）；256-bit opaque token（`secrets.token_urlsafe(32)`），DB 只存 SHA-256 摘要；默认 30 天绝对有效期，无 refresh token；logout 只撤销当前 session；同一用户多会话。
6. 认证语义：登录失败统一 `401 INVALID_CREDENTIALS`（不带 WWW-Authenticate）；用户名不存在时执行固定 dummy Argon2id 校验（测试只校验走同类 hasher，不写毫秒断言）；注册冲突 `409 USERNAME_TAKEN`；受保护请求缺失/非法/撤销/过期 token 统一 401 带 `WWW-Authenticate: Bearer`（缺失=AUTH_REQUIRED，其余=AUTH_INVALID）；跨用户资源统一 404 不暴露存在性。
7. 请求/响应形状：register/login 请求仅 `username`、`password`；成功响应 `user {user_id, username, created_at}`、`access_token`、`token_type="Bearer"`、`expires_at`；me 只返回 `user`；register 201 / login 200 / logout 204。register/login 豁免 Bearer 与幂等键；logout/me 需要 Bearer；logout 走幂等键（POST 写接口，契约 1.3 写接口统一）。client 不自动重试 register/login。
8. 限流（structure-contract §1.6，冻结）：写操作 60 req/min/user；IP 5 req/s 全部接口；register/login 按来源 IP（默认阈值运维可调）；login 按规范化用户名分桶；PUT /api-key 10/h/user；POST /samples 20/h/user；PDF 上传 10/h/user；429 RATE_LIMITED + Retry-After；认证完成后业务限流键 = user_id，保留 IP 总闸门，不得再读原始设备头。
9. 敏感信息（DESIGN §4.5）：Authorization、密码、token、password hash、legacy_device_id、API Key、完整 Prompt、模型原文不得进入日志、错误响应、测试报告或命令参数；日志身份字段 user_id；register/login/api-key 路径属敏感请求清单。
10. 归属切换（DESIGN §5.1）：全部 owner 参数从 device_id 改 user_id 或 AuthPrincipal；禁止反查「主 device_id」继续当租户键；services 与测试守住 Card↔Deck、Task↔PDF/Deck、LlmCallAttempt↔Task 的 user_id 一致性；旧 device_id 行保留不动、无访问路径（D-06，不迁不删）。
11. 禁止破坏性 git（reset/restore/clean/stash）；git add 只限本任务文件清单；不部署、不 push、不迁移生产库；不碰 docs/llm-account-long-run-v1/、docs/account-auth-test-platform-long-run-v1/（只读上游）。
12. P4 跟进清单（P3 final review 裁决，本计划必须闭环）：
    a. api_keys 每设备唯一性 DB 保障随 PK 重建丢失 → 补 `UNIQUE(device_id)` 索引迁移（Task 5）。
    b. ddc6f34e30b8 层 downgrade 带旧行无 CI 覆盖 → test 补 `command.downgrade(config, "2a391e994f93")` 断言（Task 5）。
    c. database-design §7.1 fail-closed 生效范围一句 + 「主键重建任务」段任务归属措辞修正（Task 5/6）。
    d. P3 写侧债务三条：ApiKey 用户域行对 ORM 不可见（移除 mapper 覆盖、用户域走 Core 直写或重映射——Task 5）；IdempotencyKey NULL 主键行 update/delete FlushError（本计划幂等域切换后 user_id 恒非空，无 NULL 行写入路径——Task 3 验证）；tasks.device_id 注解 Mapped[str] 收敛（Task 3/6 随调用链收紧）。
13. 双头过渡窗口纪律：Task 2/3 期间请求需同时携带 Bearer 与 X-Device-ID（测试 fixture 强制）；Task 4 删除 X-Device-ID 后全仓无 device 头注入；最终态 X-Device-ID 不参与任何普通请求认证/授权/幂等/限流。

## 现状基线（2026-08-14）

- HEAD：`1e52f96`（P3 完成）；Alembic head `a7cc699f3fd8`；全量 508 passed。
- P2 已落：errors.py 4 个账号错误码（AUTH_REQUIRED/AUTH_INVALID/INVALID_CREDENTIALS/USERNAME_TAKEN，ERROR_HTTP_STATUS + LOCALIZATION_KEYS 同步）；openapi 2.2.0 全局 BearerAuth + /auth 四路径 + 4 个 auth schema；structure-contract §1.1/§1.3/§1.4/§1.6/§3.14/§3.15/§6.11/§7 账号组。
- P3 已落：User/AuthSession 模型（models.py:503-535，user_id/session_id TEXT PK、username UNIQUE、token_hash UNIQUE、ix_auth_sessions_user_id、FK users ON DELETE CASCADE）；api_keys PK→user_id（`__mapper_args__ = {"primary_key": ["device_id"]}` 过渡）、idempotency_keys PK→(user_id, path, idempotency_key)（allow_partial_pks=True）、8 个 owner 表 user_id 列 + CHECK 双非空。
- 现状贯穿点：`app/middleware/device_id.py`（鉴权 + devices 自动注册）、`idempotency.py`（execute_idempotent 参数 device_id）、`rate_limit.py`（业务维度键读原始 X-Device-ID 头）、9 个 api handlers + 14 个 services 用 device_id；26 个测试文件注入 X-Device-ID 头；tests/conftest.py 有集中 `client` fixture（line 36）。
- app/schemas/ 尚无 auth 锚点模型；pyproject 无 argon2 依赖；Settings 无 auth TTL/限流字段。

## 文件结构（新增/删除/改造）

- 新增 `main/domain/auth.py`：AuthPrincipal（纯 dataclass，domain 层零依赖）。
- 新增 `main/services/auth/password.py`：Argon2id hasher + 生产参数守卫 + DUMMY_PASSWORD_HASH。
- 新增 `main/services/auth/tokens.py`：generate_session_token / hash_session_token。
- 新增 `main/services/auth/service.py`：register_user / login_user / logout_session / get_current_user。
- 新增 `main/app/schemas/auth.py`：AuthRegisterRequest / AuthLoginRequest / AuthUser / AuthSessionResponse 锚点。
- 新增 `main/app/api/auth.py`：四端点路由。
- 新增 `main/app/middleware/auth.py`：BearerAuthMiddleware。
- 删除 `main/app/middleware/device_id.py`（Task 4）。
- 修改：`main/app/main.py`（装配序）、`main/app/middleware/idempotency.py`（user_id 域）、`main/app/middleware/rate_limit.py`（user 维度 + auth 分桶）、`main/app/middleware/logging.py`（user_id 字段 + 敏感路径）、`main/app/config.py`（TTL/限流字段）、`main/pyproject.toml`（argon2-cffi）、9 个 api handlers、14 个 services、`main/infra/db/models.py`（ApiKey mapper 移除 + Task.device_id 注解）、新迁移 revision、`docs/Architecture/database-design.md`、`docs/Architecture/structure-contract.md`（Task 6）、`docs/Architecture/openapi.yaml`（Task 6 核对）。
- 测试：新增 `tests/unit/test_auth_password.py`、`tests/unit/test_auth_tokens.py`、`tests/integration/test_auth.py`、`tests/integration/test_auth_middleware.py`；改造 26 个含 X-Device-ID 的测试文件、conftest.py、`tests/integration/test_alembic_migration.py`。

---

### Task 1: 认证基础设施（依赖 + hasher + token + principal + Settings）

**Files:**
- Modify: `main/pyproject.toml`（dependencies 加 `argon2-cffi>=23.1.0`）
- Create: `main/domain/auth.py`
- Create: `main/services/auth/password.py`、`main/services/auth/tokens.py`
- Modify: `main/app/config.py`（+3 字段）
- Test: `main/tests/unit/test_auth_password.py`、`main/tests/unit/test_auth_tokens.py`

**Interfaces:**
- Produces（后续任务依赖，签名逐字）：
  - `domain.auth.AuthPrincipal`：`@dataclass(frozen=True, slots=True) class AuthPrincipal: user_id: str; session_id: str`
  - `services.auth.password.hash_password(password: str) -> str`
  - `services.auth.password.verify_password(password: str, password_hash: str) -> bool`
  - `services.auth.password.verify_dummy(password: str) -> bool`（对 DUMMY_PASSWORD_HASH 校验，不返回结果用途）
  - `services.auth.tokens.generate_session_token() -> str`
  - `services.auth.tokens.hash_session_token(token: str) -> str`（SHA-256 hex）

- [ ] **Step 1: 写失败测试** `tests/unit/test_auth_password.py`

```python
"""Argon2id 密码哈希（DESIGN §4.2：生产参数不低于 OWASP 基线 19456/2/1；dummy 校验）。"""
from argon2 import PasswordHasher

from services.auth.password import (
    DUMMY_PASSWORD_HASH,
    Argon2PasswordHasher,
    PRODUCTION_PARAMS,
    hash_password,
    verify_dummy,
    verify_password,
)

def test_production_params_not_below_owasp_baseline():
    assert PRODUCTION_PARAMS["memory_cost"] >= 19456
    assert PRODUCTION_PARAMS["time_cost"] >= 2
    assert PRODUCTION_PARAMS["parallelism"] >= 1

def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False

def test_password_not_truncated_or_normalized():
    # 128 字符上限内原样参与哈希（DESIGN：禁止静默截断/Unicode 归一化）
    p128 = "x" * 128
    h = hash_password(p128)
    assert verify_password(p128, h) is True
    # 不同大小写/NFC 差异字符串产生不同哈希结果（不做大小写转换）
    assert verify_password("PASSWORD", hash_password("password")) is False

def test_hasher_allows_low_cost_injection():
    low = Argon2PasswordHasher(memory_cost=8, time_cost=1, parallelism=1)
    h = low.hash("pw")
    assert Argon2PasswordHasher.verify_low_cost(h, "pw", low) is True

def test_verify_dummy_runs_same_hasher_branch():
    # 只断言走同类 hasher 分支（DESIGN：不写毫秒断言）
    assert verify_dummy("anything") is False
    assert DUMMY_PASSWORD_HASH.startswith("$argon2id$")
```

`tests/unit/test_auth_tokens.py`：

```python
"""opaque session token（DESIGN §4.3：256-bit 随机，DB 只存 SHA-256 摘要）。"""
import hashlib
import secrets

from services.auth.tokens import generate_session_token, hash_session_token

def test_token_is_256_bit_random():
    t1, t2 = generate_session_token(), generate_session_token()
    assert t1 != t2
    # token_urlsafe(32) = 256-bit → 43 字符 base64url（末位无填充）
    assert len(t1) == 43

def test_hash_is_sha256_hex_of_plaintext():
    t = generate_session_token()
    assert hash_session_token(t) == hashlib.sha256(t.encode()).hexdigest()

def test_hash_does_not_reveal_token():
    t = generate_session_token()
    h = hash_session_token(t)
    assert t not in h and h not in t
```

- [ ] **Step 2: 运行确认失败** `pytest tests/unit/test_auth_password.py tests/unit/test_auth_tokens.py -q` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

`main/domain/auth.py`（domain 层零依赖）：

```python
"""认证主体（structure-contract 1.1：显式 AuthPrincipal，禁止 device_id 伪装身份）。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    user_id: str
    session_id: str
```

`main/services/auth/password.py`：

```python
"""Argon2id 密码哈希（DESIGN §4.2 冻结契约）。

生产默认参数 = OWASP 当前最低基线 memory_cost=19456 KiB / time_cost=2 / parallelism=1；
测试可注入低成本 hasher，但生产默认与参数守卫不得被降低（Task 1 守卫测试强制）。
"""

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import VerifyMismatchError

PRODUCTION_PARAMS = {"memory_cost": 19456, "time_cost": 2, "parallelism": 1}

_PRODUCER = _Argon2PasswordHasher(**PRODUCTION_PARAMS)

# 固定 dummy 哈希（同参数哈希固定假密码，一次生成后硬编码，避免每次启动重算 19 MiB）
DUMMY_PASSWORD_HASH = _PRODUCER.hash("dummy-password-for-timing-equalization")


class Argon2PasswordHasher:
    """可注入低成本参数的 hasher（测试专用入口）；verify 与 hash 拆开便于守卫。"""

    def __init__(self, *, memory_cost: int, time_cost: int, parallelism: int) -> None:
        self._hasher = _Argon2PasswordHasher(
            memory_cost=memory_cost, time_cost=time_cost, parallelism=parallelism
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)


def hash_password(password: str) -> str:
    return _PRODUCER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _PRODUCER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def verify_dummy(password: str) -> bool:
    """用户不存在时执行固定 dummy 校验，抹平账号存在性时序差（DESIGN §4.2）。"""
    try:
        _PRODUCER.verify(DUMMY_PASSWORD_HASH, password)
        return True
    except VerifyMismatchError:
        return False
```

`main/services/auth/tokens.py`：

```python
"""opaque session token（DESIGN §4.3：256-bit 随机；DB 只存 SHA-256 摘要）。"""

import hashlib
import secrets


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
```

`main/app/config.py`（Settings 加 3 字段，紧随既有 rate_limit 组）：

```python
    auth_session_ttl_days: int = 30
    rate_limit_auth_per_hour: int = 20
    rate_limit_login_username_per_hour: int = 10
```

`main/pyproject.toml` dependencies 加 `"argon2-cffi>=23.1.0",`。

- [ ] **Step 4: 运行确认通过** 聚焦测试 + 全量 pytest 绿 + ruff/mypy/format 绿（锁定文件 `requirements-dev.lock` 随 pyproject 更新：`conda run -n shanka-backend pip install -e .[dev]` 后 `pip-compile` 或按仓库既有锁定流程）。

- [ ] **Step 5: Commit**

```bash
git add main/pyproject.toml main/domain/auth.py main/services/auth/password.py main/services/auth/tokens.py main/app/config.py main/tests/unit/test_auth_password.py main/tests/unit/test_auth_tokens.py
git commit -m "feat(account-auth): P4-1 认证基础设施——Argon2id 参数守卫 + dummy 校验 + 256-bit token 摘要 + AuthPrincipal + Settings"
```

---

### Task 2: auth 端点 + BearerAuthMiddleware 并存上线（双头窗口开始）

**Files:**
- Create: `main/app/middleware/auth.py`、`main/app/schemas/auth.py`、`main/services/auth/service.py`、`main/app/api/auth.py`
- Modify: `main/app/main.py`（装配）、`main/app/middleware/device_id.py`（豁免 /auth/register、/auth/login）、`main/tests/conftest.py`（auth_headers helper）、26 个含 X-Device-ID 的测试文件（headers 双头注入）
- Test: `main/tests/integration/test_auth.py`、`main/tests/integration/test_auth_middleware.py`

**Interfaces:**
- Consumes: Task 1 全部接口；`infra.clock.SystemClock().now_utc()` + `infra.db.session.format_utc`（时间字符串格式与全仓一致）；`infra.db.models.User/AuthSession`（P3 已建）。
- Produces：
  - `services.auth.service.register_user(session, *, username, password, now) -> tuple[dict, str, str]`（返回 (user_dict, access_token, expires_at)）
  - `services.auth.service.login_user(session, *, username, password, now) -> tuple[dict, str, str]`
  - `services.auth.service.logout_session(session, *, session_id, now) -> None`
  - `services.auth.service.get_current_user(session, *, session_id) -> dict`
  - `services.auth.service.resolve_principal(session, *, token_hash, now) -> AuthPrincipal | None`（中间件用；revoked/过期 → None）
  - `app.middleware.auth` 设置 `request.state.principal = AuthPrincipal(user_id, session_id)`

- [ ] **Step 1: 写失败测试** `tests/integration/test_auth.py`（摘录；全部用例：注册成功 201 形状、用户名冲突 409、非法用户名/密码 400、登录成功 200 形状、登录失败 401 INVALID_CREDENTIALS 且无 WWW-Authenticate、logout 204 + me 401、多会话并存、logout 只撤销当前 session、过期 session 401、register/login 幂等键豁免）

```python
"""账号端点集成（DESIGN §4.4；/auth/* 契约 structure-contract §6.11）。"""
from fastapi.testclient import TestClient

def _auth_headers(client: TestClient, username: str = "alice", password: str = "secret-pass-1"):
    r = client.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 201
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert set(body) == {"user", "access_token", "token_type", "expires_at"}
    assert set(body["user"]) == {"user_id", "username", "created_at"}
    assert body["user"]["username"] == username
    return {"Authorization": f"Bearer {body['access_token']}", "X-Device-ID": "11111111-1111-4111-8111-111111111111"}

def test_register_login_logout_me_flow(client):
    headers = _auth_headers(client)
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json() == {"user": {"user_id": me.json()["user"]["user_id"], "username": "alice", "created_at": me.json()["user"]["created_at"]}}
    # logout 只撤销当前 session
    assert client.post("/auth/logout", headers={**headers, "Idempotency-Key": "22222222-2222-4222-8222-222222222222"}).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401
    # 重新登录生成新 session
    r2 = client.post("/auth/login", json={"username": "alice", "password": "secret-pass-1"})
    assert r2.status_code == 200
    h2 = {"Authorization": f"Bearer {r2.json()['access_token']}", "X-Device-ID": "11111111-1111-4111-8111-111111111111"}
    assert client.get("/auth/me", headers=h2).status_code == 200

def test_register_username_conflict_409(client):
    _auth_headers(client, username="bob", password="pass-1234")
    r = client.post("/auth/register", json={"username": "BOB", "password": "pass-1234"})  # 转小写后冲突
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "USERNAME_TAKEN"

def test_login_failure_unified_401_invalid_credentials(client):
    _auth_headers(client, username="carol", password="pass-1234")
    r = client.post("/auth/login", json={"username": "carol", "password": "wrong-password"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert "WWW-Authenticate" not in r.headers  # INVALID_CREDENTIALS 不带该头（DESIGN §4.3）
    # 用户名不存在同样 401 同码（不暴露存在性）
    r2 = client.post("/auth/login", json={"username": "nobody", "password": "whatever-1"})
    assert r2.status_code == 401
    assert r2.json()["error"]["code"] == "INVALID_CREDENTIALS"

def test_username_validation_and_normalization(client):
    for bad in ("ab", "a" * 33, "has space", "UPPER@X", "中文名"):
        r = client.post("/auth/register", json={"username": bad, "password": "pass-1234"})
        assert r.status_code == 400, bad
    for bad_pw in ("short7!", "x" * 129):
        r = client.post("/auth/register", json={"username": "validname", "password": bad_pw})
        assert r.status_code == 400
```

`tests/integration/test_auth_middleware.py`：

```python
"""Bearer 认证中间件（DESIGN §4.3：401 + WWW-Authenticate: Bearer；豁免清单）。"""
def test_missing_token_401_auth_required(client):
    r = client.get("/decks", headers={"X-Device-ID": "11111111-1111-4111-8111-111111111111"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_REQUIRED"
    assert r.headers["WWW-Authenticate"] == "Bearer"

def test_malformed_token_401_auth_invalid(client):
    r = client.get("/decks", headers={"Authorization": "NotBearer xyz", "X-Device-ID": "11111111-1111-4111-8111-111111111111"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"
    assert r.headers["WWW-Authenticate"] == "Bearer"

def test_unknown_token_401_auth_invalid(client):
    r = client.get("/decks", headers={"Authorization": "Bearer never-seen-token", "X-Device-ID": "11111111-1111-4111-8111-111111111111"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "AUTH_INVALID"

def test_exempt_paths_do_not_require_bearer(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.post("/auth/register", json={"username": "eve", "password": "pass-1234"}).status_code == 201
    assert client.post("/auth/login", json={"username": "eve", "password": "pass-1234"}).status_code == 200
```

- [ ] **Step 2: 运行确认失败** 聚焦跑新测试 → FAIL（模块/端点不存在）

- [ ] **Step 3: 实现**

`main/app/schemas/auth.py`（红线 1 锚点，与 openapi 2.2.0 组件一致；字段名逐字）：

```python
"""账号 schema 锚点（openapi 2.2.0 AuthRegisterRequest/AuthLoginRequest/AuthUser/AuthSessionResponse）。"""
from pydantic import BaseModel, Field


class AuthRegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class AuthUser(BaseModel):
    user_id: str
    username: str
    created_at: str


class AuthSessionResponse(BaseModel):
    user: AuthUser
    access_token: str
    token_type: str
    expires_at: str
```

`main/services/auth/service.py`：

```python
"""账号用例（DESIGN §4.2/§4.3）：注册/登录/登出/当前用户/principal 解析。"""
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from domain.auth import AuthPrincipal
from infra.clock import SystemClock
from infra.db.models import AuthSession, User
from infra.db.session import format_utc
from services.auth.password import hash_password, verify_dummy, verify_password
from services.auth.tokens import generate_session_token, hash_session_token

_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
_SESSION_TTL_DAYS = 30  # 绝对有效期（Settings.auth_session_ttl_days 接线处覆盖）


def _normalize_username(username: str) -> str:
    return username.lower()


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise AppError(ErrorCode.VALIDATION_ERROR, "用户名须为 3-32 位小写字母/数字/._-")


def _validate_password(password: str) -> None:
    if len(password) < 8 or len(password) > 128:
        raise AppError(ErrorCode.VALIDATION_ERROR, "密码须为 8-128 个字符")


def _session_ttl(settings) -> timedelta:
    return timedelta(days=settings.auth_session_ttl_days)
```

实现函数逐字契约（plan 全文另附；register/login 流程：校验→规范化→dummy 或 verify→User INSERT / AuthSession INSERT→token 生成；logout：`UPDATE auth_sessions SET revoked_at=:now WHERE session_id=:sid AND revoked_at IS NULL`；resolve_principal：按 token_hash 查 session JOIN 判断 revoked/expired）。

`main/app/middleware/auth.py`（运行序在 DeviceID 外层；豁免 `/healthz /readyz /metrics /openapi.json /auth/register /auth/login`；`Authorization: Bearer <token>` 解析；401 带 `WWW-Authenticate: Bearer`）：

```python
"""Bearer 认证中间件（structure-contract 1.1 V2.2；DESIGN §4.3）。"""
_AUTH_EXEMPT_PATHS = {"/healthz", "/readyz", "/metrics", "/openapi.json", "/auth/register", "/auth/login"}

class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)
        header = request.headers.get("Authorization")
        if header is None or not header.startswith("Bearer "):
            return self._error(ErrorCode.AUTH_REQUIRED, "缺少 Bearer 凭证", www_authenticate=True)
        token = header[len("Bearer "):].strip()
        if not token:
            return self._error(ErrorCode.AUTH_REQUIRED, "缺少 Bearer 凭证", www_authenticate=True)
        token_hash = hash_session_token(token)
        now = format_utc(SystemClock().now_utc())
        with request.app.state.session_factory() as session:
            principal = resolve_principal(session, token_hash=token_hash, now=now)
        if principal is None:
            return self._error(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期", www_authenticate=True)
        request.state.principal = principal
        return await call_next(request)
```

`main/app/api/auth.py`：四端点（register 201 / login 200 / logout 204 / me 200；register/login 不走 execute_idempotent；logout 走 execute_idempotent（path="/auth/logout"）；响应 JSONResponse 直返）。`main/app/main.py`：`app.add_middleware(AuthMiddleware)` 插在 `DeviceIDMiddleware` 之后（运行序：…RateLimit → Auth → DeviceID → Logging…，注释同步）。

`main/app/middleware/device_id.py`：`_EXEMPT_PATHS` 加 `"/auth/register", "/auth/login"`（注释注明双头过渡窗口，Task 4 整体移除）。

`main/tests/conftest.py` 新增 helper（双头）：

```python
def auth_headers(client: TestClient, username: str = "alice", password: str = "secret-pass-1") -> dict[str, str]:
    """register 或 login 后返回 Bearer + 过渡期 X-Device-ID 双头。"""
    r = client.post("/auth/register", json={"username": username, "password": password})
    if r.status_code == 409:
        r = client.post("/auth/login", json={"username": username, "password": password})
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "X-Device-ID": "11111111-1111-4111-8111-111111111111"}
```

26 个测试文件机械改造：每个 `headers={"X-Device-ID": <device_id>}` 构造替换为 `headers=auth_headers(client)`（或双头显式）；device_id 变量仍可用于数据断言（v2.1 语义保持到 Task 3 才切换）。既有 device 隔离测试保留（双头窗口内仍按 device 域）。

- [ ] **Step 4: 运行确认通过** 新测试绿 + 全量 pytest 绿 + 四工具绿。

- [ ] **Step 5: Commit**

```bash
git add <Task 2 文件清单>
git commit -m "feat(account-auth): P4-2 auth 端点与 BearerAuthMiddleware 并存上线——register/login/logout/me + 401 WWW-Authenticate + 双头过渡 fixture"
```

---

### Task 3: 业务面 user_id 切换（handlers/services/幂等/限流）

**Files:**
- Modify: `main/app/middleware/idempotency.py`、`main/app/middleware/rate_limit.py`、`main/app/main.py`（运行序注释）、9 个 api handlers（decks/cards/review/stats/pdf/tasks/samples/observability；api_key 留 Task 5）、14 个 services（decks/cards(service+rewrite)/review/stats/pdf(service+scanner+parser?)/tasks(service+executor)/generation(batches/ledger/planning_executor/scoring/samples)/scheduling/scheduler?）、`main/infra/db/models.py`（Task.device_id 注解收敛）
- Test: 改造隔离语义测试（跨用户 404）、一致性守卫测试（Card↔Deck、Task↔PDF/Deck、LlmCallAttempt↔Task）

**Interfaces:**
- Consumes: `request.state.principal.user_id`（Task 2 中间件设置）；Task 1 接口。
- Produces：`execute_idempotent(session, *, user_id: str, path: str, idempotency_key: str, request_body_hash: str, fn)`（参数名 device_id→user_id，谓词与写入用 `IdempotencyKey.user_id`）；全部 services 的 `device_id` 关键字参数更名 `user_id`（签名逐字见各文件——如 `services.decks.service.create_deck(session, *, user_id, name, now)`）。

- [ ] **Step 1: 写失败测试**（摘录）

`tests/integration/test_isolation_user.py`（新建；跨用户 404 语义——替换原 device 隔离断言口径）：

```python
"""用户隔离（DESIGN §5.1：跨用户资源统一 404，不暴露存在性）。"""
def test_cross_user_deck_404(client):
    h1 = auth_headers(client, username="user1", password="pass-1111")
    h2 = auth_headers(client, username="user2", password="pass-2222")
    r = client.post("/decks", json={"name": "deck-a"}, headers={**h1, "Idempotency-Key": "33333333-3333-4333-8333-333333333333"})
    deck_id = r.json()["deck_id"]
    assert client.get(f"/decks/{deck_id}", headers=h2).status_code == 404

def test_card_deck_user_consistency(client):
    # deck 属 user1，card 挂在 user1 的 deck 下；user2 用该 deck_id 建卡 → 404 而非 400/201
    ...

def test_task_pdf_deck_user_consistency(client):
    # pdf/task 归属校验：user2 用 user1 的 file_id/deck_id 创建任务 → 404
    ...
```

幂等域切换测试（`tests/integration/test_idempotency_user_domain.py`）：同一 idempotency_key 在不同用户下互不重放（user1 创建 deck 成功；user2 同 key 同 body 正常执行非重放）；同用户同 key 重放原响应。

限流测试（`tests/integration/test_rate_limit_user.py`）：user1 写操作超限 429 不影响 user2；/auth/register IP 桶 429 + Retry-After；/auth/login 用户名桶（同用户名不同密码多次登录 → 429）。

- [ ] **Step 2: 运行确认失败** 新测试红 + 旧 device 隔离测试因语义切换预期转红（实现后转绿）

- [ ] **Step 3: 实现**（机械规则，逐文件）

1. `middleware/idempotency.py`：`execute_idempotent` 参数 `device_id`→`user_id`；三处 `IdempotencyKey.device_id ==` 谓词→`IdempotencyKey.user_id ==`；`IdempotencyKey(device_id=...)` 构造→`user_id=user_id`（新行 user_id 非空——CHECK 双非空满足；device_id 不写入。**P4 跟进 d 验证**：idempotency 写入/查询路径 user_id 恒非空，无 NULL 主键 FlushError 面——OR 测试覆盖「重放/冲突/并发占位」三路径）。docstring「并发占位 (user_id, path, key)」。
2. `middleware/rate_limit.py`：`_scope` 增加 auth 维度（`POST /auth/register|/auth/login` → "auth" 桶，键=IP；login 另加用户名桶，键=f"login:{normalized_username}"，用户名从 JSON body 解析——`request.state.raw_body` 不可用（RateLimit 在 BodyCapture 外层）→ 用 `await request.body()` 风险（读流）——改用轻量方案：仅对 login 读 header 无用户名。**裁决**：用户名桶需要 body。方案：RateLimit 运行序移入 Auth 与 Logging 之间（读 body 前不可能）→ 采用 `request.state.raw_body` 需要在 BodyCapture 内层。**最终方案**：login 用户名桶实现为「auth service 内限流」——在 `login_user` 入口检查内存桶（同 RateLimiter 复用）——限流逻辑仍集中在 middleware/rate_limit.py 提供 `AuthRateLimiter` 类，service 调用。register/login IP 桶留在 middleware（scope="auth"，键=IP）。业务维度（write/api_key/samples/pdf）键从原始 X-Device-ID 头改为 `request.state.principal.user_id`——**运行序调整**：Auth 移到 RateLimit 外层（Metrics → RequestID → Auth → RateLimit → DeviceID → Logging）；principal 未设置路径（豁免路径）不参与业务维度（scope 判定在豁免路径返回 None）。
3. `main.py`：中间件添加序改为 `AuthMiddleware` 在 `RateLimitMiddleware` 之后添加（运行序 Auth 在 RateLimit 外层）；注释同步。
4. 9 个 handlers（decks/cards/review/stats/pdfs/tasks/samples/observability）：`request.state.device_id` → `request.state.principal.user_id`（本地变量名 `user_id`）；`execute_idempotent(device_id=...)` → `user_id=...`。
5. 14 个 services：关键字参数 `device_id` → `user_id`，查询谓词 `== device_id` → `== user_id`；创建行 `device_id=user_id` 写入（v2.1 行含 device_id + user_id NULL；新行 user_id 非空 device_id NULL——**统一改**：新写入行不再生成 device_id（DESIGN §5.2「新写入必须非空且不得生成 device_id」）——即 INSERT 处 `device_id=<value>` 移除，改 `user_id=user_id`。这影响：cards/decks/pdf_files/tasks/review_events/llm_call_attempts 的构造列。CHECK 双非空满足（user_id 非空）。既有测试断言 device_id 回写的地方同步改 user_id。
6. 一致性守卫：services 层归属校验补 user_id 一致性（Card↔Deck：`cards.user_id == decks.user_id` 同事务校验；Task↔PDF/Deck 同 user_id；LlmCallAttempt↔Task user_id）——实现为「资源查询谓词直接带 user_id（跨用户自然 404）」+ 关联查询 JOIN user_id 校验（防御同 user 伪造 id 挂接）。测试覆盖见 Step 1。
7. `models.py`：`Task.device_id` 注解 `Mapped[str]` → `Mapped[str | None]`（DB 已 NULL；create_attempt 调用链 3 处按 `assert task.device_id is not None` 或传参调整——executor 后台 P5 接续前任务由用户创建 user_id 非空，device_id 为 None——**裁决**：create_attempt(device_id) 参数改 user_id（ledger 归属切换的一部分——本任务 ledger.py 已改）。收敛后 mypy 全绿。
8. 测试改造：原 device 隔离测试 → user 隔离（跨用户 404）；幂等/限流测试按新键；全量回归。

- [ ] **Step 4: 运行确认通过** 全量 pytest + 四工具绿

- [ ] **Step 5: Commit**

```bash
git add <Task 3 文件清单>
git commit -m "feat(account-auth): P4-3 业务面 user_id 切换——handlers/services/幂等域/限流键 + 跨用户 404 一致性守卫 + tasks.device_id 注解收敛"
```

---

### Task 4: X-Device-ID 退出（middleware 删除 + devices 注册停止 + fixture 去设备头）

> **边界调整（2026-08-14，implementer 上报 + 控制器裁决）**：T3 过渡期保留的 3 处 handler
> `request.state.device_id` 读取与 Key device 域解析在删除 DeviceIDMiddleware 后必
> AttributeError。本任务吸收原 T5 的「Key 解析/写侧切 user 域」：api_key service 用 Core
> 直写切 user 域（mapper 身份键仍 device_id，T5 才移除）、三处 Key 查找（tasks.service
> create_task / cards.rewrite / executor._decrypt_api_key）按 user_id、测试双列种子补丁移除。
> T5 收缩为：mapper 移除 + UNIQUE(device_id) 迁移 + ddc6 CI 断言 + database-design 2.2/§7.1
> 措辞 + E2E 判别测试。

**Files:**
- Delete: `main/app/middleware/device_id.py`
- Modify: `main/app/main.py`（装配移除 + 注释）、`main/tests/conftest.py`（auth_headers 去 X-Device-ID）、26 个测试文件（headers 去 device 头）、`main/app/middleware/CLAUDE.md`（文件清单说明）
- Test: `tests/integration/test_no_device_header.py`（新）

- [ ] **Step 1: 写失败测试**

```python
"""X-Device-ID 已退出（DESIGN §4.4：普通请求删除该头；devices 不再自动注册）。"""
def test_no_device_header_required(client):
    h = auth_headers(client)  # 仅 Bearer
    assert client.get("/decks", headers=h).status_code == 200

def test_device_header_ignored(client):
    h = auth_headers(client)
    r = client.get("/decks", headers={**h, "X-Device-ID": "99999999-9999-4999-8999-999999999999"})
    assert r.status_code == 200  # 头被忽略，不参与认证/注册

def test_devices_table_not_auto_registered(client, session_factory):
    before = session_factory().scalar(select(func.count()).select_from(Device))
    h = auth_headers(client)
    client.get("/decks", headers=h)
    after = session_factory().scalar(select(func.count()).select_from(Device))
    assert after == before  # 无自动创建/刷新
```

- [ ] **Step 2: 运行确认失败**（device_id.py 仍存在 → X-Device-ID 缺失 401）

- [ ] **Step 3: 实现** 删除 device_id.py；main.py 移除 import 与 add_middleware（运行序：Metrics → RequestID → Auth → RateLimit → Logging → BodyCapture → 路由）；conftest auth_headers 只返回 Bearer；26 文件 headers 清理；`devices` 表无新写入（ORM 保留仅兼容审计）。

- [ ] **Step 4: 运行确认通过** 全量 pytest（无 X-Device-ID 注入仍全绿）+ 四工具

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(account-auth): P4-4 X-Device-ID 退出——middleware 删除 + devices 自动注册停止 + fixture 去设备头"
```

---

### Task 5: api_key 用户域写侧 + 迁移收尾（P4 跟进 a/b/c/d）

> **边界调整（2026-08-14，控制器裁决）**：Key 解析/写侧切 user 域已在 T4 吸收（api_key
> service Core 直写、三处 Key 查找、handler principal、种子补丁移除——见 T4 节边界调整）。
> 本任务收缩为：ApiKey mapper 移除（身份键回 user_id，用户域行 ORM 可见）+ UNIQUE(device_id)
> 迁移 + ddc6 层 downgrade 带旧行 CI 断言 + database-design 2.2/§7.1 措辞 + E2E 判别测试
> （API 建任务端到端生成成功）。Core 直写路径是否回 ORM 由本任务评估（语义不变即可）。

**Files:**
- Modify: `main/infra/db/models.py`（ApiKey 移除 `__mapper_args__`；docstring 更新）、`main/services/api_key/service.py`（用户域 Core 直写/查询）、`main/app/api/api_key.py`（principal.user_id）、`docs/Architecture/database-design.md`（2.2 加 UNIQUE(device_id)、§7.1 措辞）、新迁移 revision、`main/tests/integration/test_alembic_migration.py`（ddc6 CI 断言 + UNIQUE 测试）
- Test: `tests/integration/test_api_key_user_domain.py`（新）、迁移测试增量

**Interfaces:**
- Consumes: principal.user_id；P3 模型（api_keys user_id PK、device_id NULL 遗留列、CHECK 双非空）。
- Produces：`save_key(session, *, user_id, api_key, encryption_key, client, now) -> dict`（用户域：user_id 非空、device_id NULL；PUT 覆盖=UPDATE by user_id）；`get_status(session, *, user_id, encryption_key) -> dict`（用户域查询；旧 device 域行不可见——D-06 无访问路径）。

- [ ] **Step 1: 写失败测试**

```python
"""api_key 用户域（P3 债务闭环：用户域行 ORM 可见；旧 device 域行无访问路径）。"""
def test_put_and_status_user_domain(client):
    h = auth_headers(client)
    r = client.put("/api-key", json={"api_key": "sk-test-abcd1234"}, headers={**h, "Idempotency-Key": "44444444-4444-4444-8444-444444444444"})
    assert r.status_code == 200
    st = client.get("/api-key/status", headers=h)
    assert st.status_code == 200
    assert st.json()["status"] == "AVAILABLE"  # 或按 mock 语义断言

def test_cross_user_key_isolation(client):
    h1 = auth_headers(client, username="keyuser1", password="pass-1111")
    h2 = auth_headers(client, username="keyuser2", password="pass-2222")
    client.put("/api-key", json={"api_key": "sk-test-aaaa1111"}, headers={**h1, "Idempotency-Key": "55555555-5555-4555-8555-555555555555"})
    st = client.get("/api-key/status", headers=h2)
    assert st.json()["status"] == "UNKNOWN"  # 用户域隔离

def test_alembic_api_keys_device_unique_added(client):  # 迁移测试文件内
    # upgrade 后 api_keys 存在 UNIQUE(device_id)（origin='u'），用户域行 device_id NULL 多行不冲突
    ...
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

1. `models.py`：ApiKey 删除 `__mapper_args__ = {"primary_key": ["device_id"]}`（身份键回 user_id 元数据 PK）；docstring 改写（用户域行 ORM 可见；旧 device 域行 user_id NULL——SQLite 多 NULL 不冲突；P3 过渡 mapper 移除）。
2. `services/api_key/service.py`：`save_key` 用户域——INSERT（user_id 非空、device_id NULL）→ 覆盖路径 `UPDATE api_keys SET ... WHERE user_id = :user_id`（Core 条件 UPDATE，synchronize_session=False——P3 同款已验证）；`get_status` 谓词 `user_id == user_id`（旧 device 域行不可见）。
3. `app/api/api_key.py`：`user_id: str = request.state.principal.user_id`；execute_idempotent(user_id=...)。
4. 新迁移 revision（`alembic revision -m "api keys device unique"`，down_revision=实测 head a7cc699f3fd8）：upgrade `CREATE UNIQUE INDEX ix_api_keys_device_id_uq ON api_keys (device_id)`（SQLite UNIQUE 多 NULL 不冲突——用户域行 device_id NULL 任意多行）；downgrade drop index。**注意**：batch 或 `op.create_index(unique=True)` 均可；SQLite create_index 不需要 batch。数据库-design 2.2 同步加「`UNIQUE (device_id)`（遗留设备域防重；用户域行 device_id NULL 多行不冲突）」行。
5. `database-design.md` §7.1：补「fail-closed 自 a7cc699f3fd8 起生效」一句（跟进 c）；「主键重建任务」段任务归属措辞修正（review_events 降级+CHECK 实际 T1 ddc6f34e30b8 落地）（跟进 c）。
6. `test_alembic_migration.py`：`test_alembic_empty_and_legacy_only_downgrade_ok` 补 `command.downgrade(config, "2a391e994f93")` + 行数断言（跟进 b）；新 UNIQUE 测试（origin='u' 列集 {device_id}）。
7. alembic check 零漂移；守卫全绿。

- [ ] **Step 4: 运行确认通过** 全量 pytest + 四工具 + 临时库 alembic upgrade head/check

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(account-auth): P4-5 api_key 用户域写侧——mapper 移除/Core 直写 + UNIQUE(device_id) 迁移 + ddc6 CI 断言 + §7.1 措辞"
```

---

### Task 6: 日志/metrics/敏感脱敏 + 契约收尾 + 全量回归

**Files:**
- Modify: `main/app/middleware/logging.py`（身份字段 user_id；Authorization 头绝不记录——现有实现不记 headers，加断言测试）、`main/app/errors.py`（移除 DEVICE_ID_REQUIRED/DEVICE_ID_INVALID + 两处派生表）、`docs/Architecture/structure-contract.md`（ch7 设备组移除 + §1.1/§8.1 设备残留核对）、`docs/Architecture/database-design.md`（§2.1 devices 描述「仅兼容审计，不再由普通请求创建/刷新」）、`docs/Architecture/openapi.yaml`（核对无 DeviceIdAuth 残留）
- Test: `tests/integration/test_sensitive_redaction.py`（新）、契约守卫更新同步

- [ ] **Step 1: 写失败测试**

```python
"""敏感信息脱敏（DESIGN §4.5：Authorization/密码/token/hash 不进日志与错误响应）。"""
def test_authorization_header_never_logged(client, caplog):
    h = auth_headers(client)
    with caplog.at_level("INFO"):
        client.get("/decks", headers=h)
    assert "Bearer" not in caplog.text
    assert "secret-pass" not in caplog.text

def test_login_failure_does_not_log_password_or_username(client, caplog):
    with caplog.at_level("INFO"):
        client.post("/auth/login", json={"username": "nosuchuser", "password": "topsecret-123"})
    assert "topsecret-123" not in caplog.text

def test_log_identity_field_is_user_id(client, caplog):
    h = auth_headers(client)
    with caplog.at_level("INFO"):
        r = client.get("/decks", headers=h)
    # 访问日志 user_id 字段存在且为注册用户 id（无 device_id 字段）
    assert "user_id" in caplog.text
    assert "device_id" not in caplog.text
```

契约守卫更新：错误码守卫期望集合移除设备两码（structure-contract ch7 与 errors.py 同批）。

- [ ] **Step 2: 运行确认失败**（设备码仍在 errors.py → 守卫全等红；日志字段未改）

- [ ] **Step 3: 实现**

1. `logging.py`：extra 字段 `device_id` → `user_id`（`getattr(request.state, "principal", None)` 存在则 `principal.user_id`，否则省略）；rate_limit.py 的 `_rate_limited` extra 同步。敏感路径清单（/auth/register /auth/login /api-key）不额外记录任何 body/header（现有 BodyCapture 只给幂等 hash，天然满足——加注释固化）。
2. `errors.py`：删除 DEVICE_ID_REQUIRED / DEVICE_ID_INVALID（ERROR_HTTP_STATUS 与 LOCALIZATION_KEYS 同步）；structure-contract ch7 设备组两行删除（含「V2.1 遗留」标注行）——守卫全等自动校验。
3. `structure-contract.md` §1.1 设备残留（如有「X-Device-ID」字样仅允许出现在历史/迁移描述）、§8.1 日志字段核对。
4. `database-design.md` §2.1 devices 表描述改为「仅兼容审计（旧 device_id 行残留）；普通请求不再自动创建/刷新」。
5. `openapi.yaml`：grep 核对无 DeviceIdAuth 引用（P2 已切全局 Bearer）；若 components 遗留 securityScheme 删除并跑 schema 守卫。
6. 敏感 sentinel 扫描（WORKER_PROMPT 验证 8）：注册一个含唯一 sentinel 密码的用户（仅测试 fixture），扫描日志/测试输出无泄漏。
7. 全量 pytest + ruff + format + mypy + alembic upgrade head/check（临时库）+ 守卫全绿。

- [ ] **Step 4: 运行确认通过** 四工具全绿 + alembic check

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(account-auth): P4-6 日志 user_id/敏感脱敏 + 设备错误码移除 + 契约收尾 + 全量回归"
```

---

## Self-Review

1. **Spec coverage**（TASKS.md P4 checklist）：register/login/logout/me 端点（T2）✓；Argon2id 生产参数守卫（T1）✓；dummy 校验（T1/T2）✓；256-bit opaque token + SHA-256 摘要（T1/T2）✓；30 天有效期（T1 Settings/T2 service）✓；logout 只撤销当前 session（T2）✓；AuthPrincipal + Bearer 401 + WWW-Authenticate（T1/T2）✓；跨用户统一 404（T3）✓；全部 owner roots 与幂等域切换 user_id（T3/T5）✓；X-Device-ID 退出普通认证/授权/幂等/限流（T3/T4）✓；IP + 用户名限流（T3）✓；敏感路径统一脱敏（T6）✓；P4 跟进 a/b/c/d（T3/T5）✓。
2. **Placeholder scan**：无 TBD/TODO；机械规则均给出精确映射；代表性代码块覆盖全部新接口；重复步骤引用接口表而非「类似 Task N」。
3. **Type consistency**：`AuthPrincipal`/`hash_password`/`resolve_principal`/`execute_idempotent(user_id=...)`/`save_key(user_id=...)` 各任务签名一致；`auth_headers(client, username, password)` 在 T2-T6 全程一致。
