# Email 登录 + 错误提示 UX + 长期登录 + 平台 backlog 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端账号体系切换为 email 登录键（username 降级为展示名）+ 会话滑动续期实现长期登录；前端登录/注册表单改造 + 全量错误码映射 + 3 秒倒计时错误提示；平台 429 重试与 PLANNING 预算前提修正。

**Architecture:** 后端沿 app → services → infra 分层：迁移加 email 列并清空存量账号，service 层校验/规范化/限流键全部切 email，auth 中间件在 resolve 成功后按天节流滑动续期。前端在现有 AuthViewModel 状态机内扩展映射表，AuthScreen 层加倒计时组件。平台 bootstrap 层做 429 显式重试，cost 预算按 3 规划组推导。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（Python 3.12，conda env shanka-backend）；Android Compose Kotlin（frontend-app 嵌套 git 仓库）；test-platform 纯 stdlib。

**Spec:** `docs/superpowers/specs/2026-08-14-email-login-and-error-ux-design.md`（唯一需求来源，任务引用其节号）

## Global Constraints

- 环境：全部 Python 命令用 `conda run -n shanka-backend ...`；测试命令 `cd main && conda run -n shanka-backend python -m pytest -q`。
- 无破坏性 git：禁 push/PR/fork/force/reset --hard；frontend-app 提交用 `git -C /home/kbzz1/shanka_backend/frontend-app commit`。
- 凭据纪律：DEEPSEEK_API_KEY/token/密码绝不进日志、报告、命令参数；.env 权限 600 且被 Git 忽略。
- 不触碰 `docs/llm-account-long-run-v1/`、`docs/account-auth-test-platform-long-run-v1/` 历史记录。
- 后端 uvicorn（127.0.0.1:8000）运行中，是线上源站——**不要停**。
- 分层单向：app → services → infra，均可依赖 domain；handler 禁止暴露 ORM 对象。
- 红线 1（schemas↔openapi↔structure-contract）与红线 2（ORM↔database-design）在**每个提交内**守卫全绿（T1 含 database-design、T2 含 openapi，即为此）。
- 每个 implementer 提交前在 main/ 跑 `conda run -n shanka-backend python -m ruff format --check .` 与 `conda run -n shanka-backend python -m ruff check .`；pre-commit 已装（ruff-format→ruff→mypy）。
- 前端 gradle：WSL 内若无 JDK 报 `JAVA_HOME is not set`，按 frontend-app/CLAUDE.md 的 Windows 侧绕法执行；T14 三面回归已验证 `./gradlew test` 53/53 可用，直接跑即可。
- SDD 派发按序单任务执行；每任务报告写入 `.superpowers/sdd/2026-08-14-email-login-and-error-ux/task-N-report.md`。

---

### Task 1: 迁移 + users 模型（email 列 / username 去唯一 / 清空账号数据）

**Files:**
- Modify: `main/infra/db/models.py:400-413`（User 模型）
- Create: `main/migrations/versions/<new>_v2_4_email_login.py`（`alembic revision -m "v2_4_email_login"` 生成）
- Modify: `main/tests/integration/test_alembic_migration.py:92-113` + legacy seed 区（~414 行）
- Modify: `docs/Architecture/database-design.md:298-330`（2.15/2.16 节）+ §7.1 落地记录

**Interfaces:**
- Consumes: 迁移链 head `b92357b079ca`（7 revisions 线性）；models.py User 现状（uq_users_username）
- Produces: `User.email: Mapped[str]`（NOT NULL）、`uq_users_email` 唯一约束；迁移后 users/auth_sessions 及下游 12 表为空

- [ ] **Step 1: 改写迁移测试断言（先红）**

`test_alembic_migration.py` 的 `test_alembic_users_auth_sessions_columns`（92-113 行）改为：

```python
def test_alembic_users_auth_sessions_columns(alembic_env: tuple[Config, Path]) -> None:
    """P3-T1：users/auth_sessions 列集合与约束（email/token_hash UNIQUE、user_id FK）。"""
    cfg, db_path = alembic_env
    with sqlite3.connect(db_path) as conn:
        users = {r[1]: r for r in conn.execute(text("PRAGMA table_info('users')"))}
        users_sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'")
        ).scalar()
    assert set(users) == {"user_id", "username", "email", "password_hash", "created_at", "updated_at"}
    assert users["user_id"][5] == 1  # PK
    assert users["username"][3] == 1  # NOT NULL
    assert users["email"][3] == 1  # NOT NULL
    assert users["password_hash"][3] == 1
    assert users["created_at"][3] == 1 and users["updated_at"][3] == 1
    assert "uq_users_email" in users_sql  # users.email UNIQUE（V2.4 登录键）
    assert "uq_users_username" not in users_sql  # V2.4 username 去唯一（展示名）
    # 其余 auth_sessions 断言（token_hash UNIQUE / user_id FK）原样保留
```

同文件追加新测试（沿用该文件 legacy 旧库副本 seed 模式，见 ~414 行 INSERT users 处）：

```python
def test_v2_4_account_data_wiped_and_downgrade_rejected(alembic_env: tuple[Config, Path]) -> None:
    """V2.4：升级清空 users/auth_sessions 及下游数据；downgrade 显式拒绝（fail-closed）。"""
    # 用文件内既有 legacy 模式建 V2.3 旧库（含 users/auth_sessions 行 + 一张下游表行，如 decks）
    # 升级到 head 后：
    #   SELECT count(*) FROM users == 0；FROM auth_sessions == 0；FROM decks == 0
    #   PRAGMA table_info('users') 含 email 列
    # 再验证 downgrade：alembic downgrade -1 → 抛 RuntimeError（迁移文件 downgrade 第一行 raise）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_alembic_migration.py -q`
Expected: FAIL（users 无 email 列 / uq_users_username 仍存在 / 新测试缺失迁移行为）

- [ ] **Step 3: 写迁移文件**

`cd main && conda run -n shanka-backend alembic revision -m "v2_4_email_login"`，文件内容：

```python
"""v2_4_email_login

users 加 email（登录键，NOT NULL + UNIQUE）；username 去唯一（降为展示名）；
清空账号及下游数据（用户裁决：存量测试账号清空重来）。downgrade 显式拒绝（fail-closed）。

Revision ID: <alembic 生成>
Revises: b92357b079ca
"""
import sqlalchemy as sa
from alembic import op

revision = "<alembic 生成>"
down_revision = "b92357b079ca"
branch_labels = None
depends_on = None

# 按 user 隔离的下游表（依赖序无关——env.py 迁移连接层 FK 关闭，P3 已验证；
# 顺序仍按依赖子→父写出以自证）
_USER_DOMAIN_TABLES = (
    "chapters", "batches", "knowledge_points", "review_states", "review_events",
    "cards", "decks", "tasks", "pdf_files", "llm_call_attempts",
    "api_keys", "idempotency_keys",
)


def upgrade() -> None:
    # 1) 清空账号及下游数据（V2.4 决策：登录键切换，存量测试账号清空重来）
    for table in _USER_DOMAIN_TABLES:
        op.execute(f"DELETE FROM {table}")
    op.execute("DELETE FROM auth_sessions")
    op.execute("DELETE FROM users")
    # 2) users：username 去唯一 + 加 email（batch 重建；表已空，NOT NULL 直加）
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_username", type_="unique")
        batch_op.add_column(sa.Column("email", sa.String(), nullable=False))
        batch_op.create_unique_constraint("uq_users_email", ["email"])


def downgrade() -> None:
    raise RuntimeError(
        "V2.4 起账号数据已清空且 email 为登录键，迁移不可逆；回退请恢复升级前备份"
    )
```

（revision id 用 alembic 生成值替换两处占位。）

- [ ] **Step 4: 改 models.py User**

```python
class User(Base):
    """2.15 users：账号数据主体（V2.2，决策 D-05；user_id 为数据主体隔离键）。

    email 为登录键（服务端 lowercase 规范化，UNIQUE）；username 为展示名
    （1-24 位中文/字母/数字/._-，可重名）；password_hash 为 Argon2id 输出，
    绝不进入日志/响应。
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
```

- [ ] **Step 5: 同步 database-design.md（红线 2，本提交内绿）**

2.15 users 节：表头加 `email` 行（`string | ✓ | 登录键；服务端转小写规范化；UNIQUE（uq_users_email）`）；`username` 行说明改为「展示名：1~24 位，中文/字母/数字/._-，可重名（无 UNIQUE）」；约束行改为 uq_users_email。2.16 auth_sessions 节末尾追加一句：「V2.4 起 expires_at 支持滑动续期（活跃续期至 now+30 天，见 structure-contract 6.11）。」§7.1 追加 V2.4 落地记录行（revision id、email 列、清空数据、downgrade 拒绝）。

- [ ] **Step 6: 跑迁移测试 + 守卫 + 漂移检查**

Run:
```bash
cd main && conda run -n shanka-backend python -m pytest tests/integration/test_alembic_migration.py -q
conda run -n shanka-backend python -m pytest tests/integration/test_orm_database_guard.py -q
conda run -n shanka-backend alembic check
```
Expected: 全 PASS + "No new upgrade operations detected"

- [ ] **Step 7: 开发库实迁（本机验收路径）**

Run: `cd main && conda run -n shanka-backend alembic upgrade head`
验证：`conda run -n shanka-backend python -c "import sqlite3; c=sqlite3.connect('data/shanka.db'); print(c.execute(\"SELECT count(*) FROM users\").fetchone(), c.execute(\"PRAGMA table_info('users')\").fetchall())"` → users count=0 且含 email 列。**不删除 data/shanka.db**；0 字节干扰文件 main/shanka.db 不碰。

- [ ] **Step 8: Commit**

```bash
git add main/infra/db/models.py main/migrations/versions/ docs/Architecture/database-design.md main/tests/integration/test_alembic_migration.py
git commit -m "feat(auth): users 加 email 登录键 + username 降为展示名 + 清空存量账号（V2.4 迁移）"
```

---

### Task 2: 注册/登录契约切换 email（errors / schemas / service / api / openapi）

**Files:**
- Modify: `main/app/errors.py:13-73`（EMAIL_TAKEN 加、USERNAME_TAKEN 删、状态映射）
- Modify: `main/app/schemas/auth.py`（AuthRegisterRequest/AuthLoginRequest）
- Modify: `main/services/auth/service.py`（校验/规范化/桶键/文案）
- Modify: `main/app/api/auth.py:59-103`（payload.email、limiter 改名）
- Modify: `main/app/main.py:155-165`（login_username_limiter → login_email_limiter）
- Modify: `main/app/config.py:43`（rate_limit_login_username_per_hour → rate_limit_login_email_per_hour，额度 10 不变）
- Modify: `docs/Architecture/openapi.yaml:1783-1830`（auth schemas）
- Modify: `main/tests/integration/test_auth.py`（全量改写）+ `main/tests/integration/test_rate_limit_user.py`（桶键引用）

**Interfaces:**
- Consumes: Task 1 的 `User.email` 列；`RateLimiter.check(key)`（rate_limit.py:65）；app.state 装配（main.py）
- Produces: `register_user(session, *, username, email, password, now, ttl_days)` / `login_user(session, *, email, password, now, ttl_days, email_limiter)`；错误码 `EMAIL_TAKEN`（409）；state 属性 `login_email_limiter`

- [ ] **Step 1: errors.py 错误码切换**

```python
    # 账号（V2.2，决策 D-05；401 一律携带 WWW-Authenticate: Bearer）
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    EMAIL_TAKEN = "EMAIL_TAKEN"
```

删除 `USERNAME_TAKEN = "USERNAME_TAKEN"` 行；`ERROR_HTTP_STATUS` 同步：删 `ErrorCode.USERNAME_TAKEN: 409`，加 `ErrorCode.EMAIL_TAKEN: 409`。全仓 grep `USERNAME_TAKEN` 清零（含 `main/migrations/versions/ddc6f34e30b8_account_auth_data_foundation.py:57` 注释——历史迁移文件注释不动，属历史记录；运行时引用必须清零）。

- [ ] **Step 2: schemas/auth.py**

```python
class AuthRegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=24)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class AuthLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
```

- [ ] **Step 3: service.py 校验与规范化**

替换 36-55 行区域：

```python
_USERNAME_RE = re.compile(r"^[\w.\-]{1,24}$")  # Unicode 字母数字（含中文）/._-，1-24 位
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")  # 宽松：含 @、无空白；长度上限由 schema 254 保证


def _normalize_username(username: str) -> str:
    return username.strip()  # 展示名：只 trim，不再强制小写


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise AppError(ErrorCode.VALIDATION_ERROR, "用户名须为 1-24 位中文/字母/数字/._-")


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.fullmatch(email):
        raise AppError(ErrorCode.VALIDATION_ERROR, "邮箱格式不正确")


def _validate_password(password: str) -> None:
    if len(password) < 8 or len(password) > 128:
        raise AppError(ErrorCode.VALIDATION_ERROR, "密码须为 8-128 个字符")


def _invalid_credentials() -> AppError:
    return AppError(ErrorCode.INVALID_CREDENTIALS, "邮箱或密码错误")
```

`register_user` 改为：

```python
def register_user(
    session: Session,
    *,
    username: str,
    email: str,
    password: str,
    now: datetime,
    ttl_days: int,
) -> tuple[dict[str, str], str, str]:
    """注册：创建用户 + 首个会话；返回 (user_dict, access_token, expires_at)。"""
    normalized_username = _normalize_username(username)
    normalized_email = _normalize_email(email)
    _validate_username(normalized_username)
    _validate_email(normalized_email)
    _validate_password(password)
    existing = session.scalar(select(User).where(User.email == normalized_email))
    if existing is not None:
        raise AppError(ErrorCode.EMAIL_TAKEN, "邮箱已被占用")
    user_id = str(uuid.uuid4())
    created_at = format_utc(now)
    session.add(
        User(
            user_id=user_id,
            username=normalized_username,
            email=normalized_email,
            password_hash=hash_password(password),
            created_at=created_at,
            updated_at=created_at,
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        # 并发注册同 email：UNIQUE 约束兜底 → 409
        raise AppError(ErrorCode.EMAIL_TAKEN, "邮箱已被占用") from exc
    token, expires_at = _create_session(session, user_id=user_id, now=now, ttl_days=ttl_days)
    user_dict = {"user_id": user_id, "username": normalized_username, "created_at": created_at}
    return user_dict, token, expires_at
```

`login_user` 改为（签名 `username_limiter` → `email_limiter`；查询与桶键切 email；其余 dummy 抹平/损坏哈希兜底逻辑原样）：

```python
def login_user(
    session: Session,
    *,
    email: str,
    password: str,
    now: datetime,
    ttl_days: int,
    email_limiter: RateLimiter,
) -> tuple[dict[str, str], str, str]:
    """登录：校验凭据 + 新建会话；失败统一 INVALID_CREDENTIALS（不暴露用户存在性）。

    email_limiter：login email 桶（P4-3→V2.4 桶键改 email）——规范化后先 check，
    超限抛 RATE_LIMITED；成功与失败登录均计入桶。
    """
    normalized_email = _normalize_email(email)
    _validate_email(normalized_email)
    allowed, _retry_after = email_limiter.check(normalized_email)
    if not allowed:
        raise AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试")
    user = session.scalar(select(User).where(User.email == normalized_email))
    if user is None:
        verify_dummy(password)  # 固定 dummy 校验抹平账号存在性时序差（DESIGN §4.2）
        raise _invalid_credentials()
    if not _verify_or_false(password, user.password_hash):
        raise _invalid_credentials()
    token, expires_at = _create_session(session, user_id=user.user_id, now=now, ttl_days=ttl_days)
    user_dict = {
        "user_id": user.user_id,
        "username": user.username,
        "created_at": user.created_at,
    }
    return user_dict, token, expires_at
```

文件顶部 docstring 同步（「用户名冲突 → 409 USERNAME_TAKEN」→「email 冲突 → 409 EMAIL_TAKEN」等）。

- [ ] **Step 4: api/auth.py + main.py + config.py 接线**

api/auth.py register_endpoint：`register_user(session, username=payload.username, email=payload.email, ...)`；login_endpoint：`login_user(session, email=payload.email, ...)`、limiter 取 `request.app.state.login_email_limiter`。文件 docstring「login 用户名桶」→「login email 桶」。

main.py：`app.state.login_username_limiter = RateLimiter(...)` → `app.state.login_email_limiter = RateLimiter(limit=settings.rate_limit_login_email_per_hour, window_seconds=3600)`；注释同步。config.py：`rate_limit_login_username_per_hour: int = 10` → `rate_limit_login_email_per_hour: int = 10`。grep `login_username` / `rate_limit_login_username` 全仓（main/）运行时引用清零。

- [ ] **Step 5: openapi.yaml 同步（红线 1，本提交内绿）**

```yaml
    AuthRegisterRequest:
      type: object
      required: [username, email, password]
      properties:
        username:
          type: string
          minLength: 1
          maxLength: 24
          description: 展示名（中文/字母/数字/._-，可重名）
        email:
          type: string
          minLength: 3
          maxLength: 254
          description: 登录键；服务端转小写规范化
        password:
          type: string
          minLength: 8
          maxLength: 128
    AuthLoginRequest:
      type: object
      required: [email, password]
      properties:
        email:
          type: string
          minLength: 3
          maxLength: 254
        password:
          type: string
          minLength: 8
          maxLength: 128
```

AuthUser 的 username 描述同步为展示名语义（3.14 对齐）。当前 openapi 为 2.2.0，版本号按文件头部惯例 bump（如 2.4.0）并保持与 structure-contract 引用一致。

- [ ] **Step 6: 改写 test_auth.py（先写测试再实现已互为 TDD——本步改完跑全绿）**

`_auth_headers` 改为：

```python
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
```

测试逐一改写（其余断言逻辑不变，只换字段/码）：

- `test_register_username_conflict_409` → `test_register_email_conflict_409`：
  `_auth_headers(client, username="bob", email="bob@example.com")`；再次注册
  `{"username": "bob2", "email": "BOB@EXAMPLE.COM", "password": "..."}` → 409 `EMAIL_TAKEN`
  （大小写变体冲突）；再注册 `{"username": "bob", "email": "other@example.com", ...}` → 201（同 username 不同 email 允许——展示名可重名）。
- `test_login_failure_unified_401_invalid_credentials`：login body `{"email": "carol@example.com", ...}`；错误 message 断言加 `"邮箱或密码错误"`；不存在邮箱同 401 同码。
- `test_username_validation_and_normalization` → `test_username_email_validation`：
  用户名非法例：`("", "a" * 25, "has space", "😀")` → 400；合法例（新语义）：`"中文名"`、`"Tom"`（大写不再被强制小写）注册成功 201 且 me 返回原样；email 非法例：`"no-at"`、`"a@b c"`、`"x" * 255` → 400。
- `test_login_success_200_shape` / `test_multiple_sessions_coexist_logout_only_current`：login body 改 `{"email": "alice@example.com", ...}`。
- `test_expired_session_401_auth_invalid` 的 DB 查询 `WHERE username = 'alice'` → `WHERE email = 'alice@example.com'`。
- 其余测试（logout 幂等/豁免/me 竞态）经 `_auth_headers` 自动适配，仅核对通过。

test_rate_limit_user.py：grep 其中 login 桶测试，username 桶键断言改 email（测试名与键同步改）。

- [ ] **Step 7: 全量后端测试 + lint**

Run:
```bash
cd main && conda run -n shanka-backend python -m pytest -q
conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .
```
Expected: 全 PASS（pytest 数量随改写微调，FAIL=0）。

- [ ] **Step 8: Commit**

```bash
git add main/app/errors.py main/app/schemas/auth.py main/services/auth/service.py main/app/api/auth.py main/app/main.py main/app/config.py main/tests/ docs/Architecture/openapi.yaml
git commit -m "feat(auth): 注册三字段 + email 登录键 + EMAIL_TAKEN（契约 V2.4）"
```

---

### Task 3: 滑动续期（service + auth 中间件）

**Files:**
- Modify: `main/services/auth/service.py`（新增 renew_session_if_due）
- Modify: `main/app/middleware/auth.py:48-53`（resolve 后接线）
- Modify: `main/tests/integration/test_auth.py`（新增滑动续期测试）

**Interfaces:**
- Consumes: Task 2 的 login/register；`resolve_principal(session, *, token_hash, now) -> AuthPrincipal | None`；`request.app.state.settings.auth_session_ttl_days`（30）
- Produces: `renew_session_if_due(session, *, session_id, now, ttl_days) -> None`（每会话每天至多一次 UPDATE）

- [ ] **Step 1: service.py 新增续期函数（`_create_session` 之后）**

```python
def renew_session_if_due(
    session: Session, *, session_id: str, now: datetime, ttl_days: int
) -> None:
    """滑动续期（V2.4）：剩余有效期不足 1 天时延长到 now + ttl_days。

    节流：仅在 expires_at < now + (ttl_days - 1) 天 时写库 → 每会话每天至多一次
    UPDATE；活跃用户永不过期，连续 ttl_days 天无请求的会话仍自然过期。
    调用前提：resolve_principal 已确认会话未撤销未过期。
    """
    threshold = format_utc(now + timedelta(days=ttl_days - 1))
    session.execute(
        update(AuthSession)
        .where(AuthSession.session_id == session_id, AuthSession.expires_at < threshold)
        .values(expires_at=format_utc(now + timedelta(days=ttl_days)))
    )
```

- [ ] **Step 2: middleware/auth.py 接线**

```python
        token_hash = hash_session_token(token)
        now_utc = SystemClock().now_utc()
        now = format_utc(now_utc)
        with request.app.state.session_factory() as session:
            principal = resolve_principal(session, token_hash=token_hash, now=now)
            if principal is None:
                return self._error(ErrorCode.AUTH_INVALID, "会话无效、已撤销或已过期")
            renew_session_if_due(
                session,
                session_id=principal.session_id,
                now=now_utc,
                ttl_days=request.app.state.settings.auth_session_ttl_days,
            )
            session.commit()
```

模块 docstring 追加一行：「- V2.4 滑动续期：resolve 成功后 renew_session_if_due 按天节流延长 expires_at（活跃永不过期）。」

- [ ] **Step 3: 测试（test_auth.py 追加）**

```python
def test_sliding_renewal_extends_near_expiry_session(client: TestClient, tmp_path: Path) -> None:
    """滑动续期：剩余 <1 天的会话经任一受保护请求后延长至 ~now+30 天。"""
    headers = _auth_headers(client, email="rene@example.com")
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    # 把会话拨到「剩余 12 小时」：expires_at = now + 0.5d
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE auth_sessions SET expires_at = datetime('now', '+12 hours')"
        ))
    assert client.get("/auth/me", headers=headers).status_code == 200
    with engine.connect() as conn:
        expires = conn.execute(text("SELECT expires_at FROM auth_sessions")).scalar()
    # format_utc 输出为 ISO Z 后缀；比较用 SQL：剩余应 > 29 天（已续到 ~30 天）
    with engine.connect() as conn:
        remaining_days = conn.execute(
            text("SELECT (julianday(expires_at) - julianday('now')) FROM auth_sessions")
        ).scalar()
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
    assert client.post("/auth/logout", headers=headers).status_code == 204
    engine = create_db_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    with engine.connect() as conn:
        revoked_at = conn.execute(text("SELECT revoked_at FROM auth_sessions")).scalar()
    assert revoked_at is not None
    assert client.get("/auth/me", headers=headers).status_code == 401
```

（时间断言若与 SQLite datetime 精度打架，改用 `SELECT expires_at > datetime('now', '+29 days')` 布尔断言。）

- [ ] **Step 4: 跑测试**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_auth.py -q && conda run -n shanka-backend python -m pytest tests/integration/test_auth_middleware.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add main/services/auth/service.py main/app/middleware/auth.py main/tests/integration/test_auth.py
git commit -m "feat(auth): 会话滑动续期——活跃用户永不过期（按天节流）"
```

---

### Task 4: 前端表单接线（BackendClient / AuthRepository / AuthViewModel / AppViewModel / AuthScreen）

**Files:**
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/data/remote/RemoteFlashcards.kt:160-182,305-344`
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthViewModel.kt:77-101`
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AppViewModel.kt:244-258`
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AuthScreen.kt:129-147（登录屏文案）,302-325（注册屏）`
- Test: `frontend-app/Front/app/src/test/java/com/qiuzhao/flashcards/ui/auth/AuthViewModelTest.kt`、`.../data/remote/AuthClientContractTest.kt`、`.../data/session/InMemorySessionStore.kt`（fake 若涉 register 签名）

**Interfaces:**
- Consumes: 后端新契约（Task 2）：register `{username,email,password}`、login `{email,password}`、错误码 EMAIL_TAKEN
- Produces: `AuthRepository.register(username, email, password)` / `login(email, password)`；`AuthViewModel.submitRegister(username, email, password)` / `submitLogin(email, password)`；`AuthViewModel.passwordsMatch(password, confirmation)` 纯函数；`AppViewModel.register(username, email, password, confirmation, onResult)`

- [ ] **Step 1: BackendClient + AuthRepository 签名切换**

RemoteFlashcards.kt 166-182 区域：

```kotlin
    /**
     * The four auth endpoints. register/login are unauthenticated and deliberately skip the
     * idempotency key (contract FR-19: no automatic retry that could silently create sessions);
     * logout sends the explicit token so it works even when the store was replaced; me is a
     * plain session-authenticated read.
     */
    suspend fun register(username: String, email: String, password: String): HttpResult = request(
        "register", "POST", "/auth/register",
        JSONObject().put("username", username).put("email", email).put("password", password).toString(),
        idempotent = false, authenticate = false
    )

    suspend fun login(email: String, password: String): HttpResult = request(
        "login", "POST", "/auth/login", credentialsBody(email, password),
        idempotent = false, authenticate = false
    )

    suspend fun logout(token: String): HttpResult = request("logout", "POST", "/auth/logout", token = token)

    suspend fun me(): HttpResult = request("me", "GET", "/auth/me")

    /** Credentials travel only in the request body and never reach a log line. */
    private fun credentialsBody(email: String, password: String): String =
        JSONObject().put("email", email).put("password", password).toString()
```

AuthRepository 接口与 RemoteFlashcardRepository override 同步：

```kotlin
interface AuthRepository {
    suspend fun register(username: String, email: String, password: String): ApiResult<Session>
    suspend fun login(email: String, password: String): ApiResult<Session>
    ...
}
override suspend fun register(username: String, email: String, password: String): ApiResult<Session> =
    sessionResult(client.register(username, email, password))

override suspend fun login(email: String, password: String): ApiResult<Session> =
    sessionResult(client.login(email, password))
```

- [ ] **Step 2: AuthViewModel 三参数 + passwordsMatch**

```kotlin
    fun login(email: String, password: String) {
        if (email.isBlank() || password.isBlank()) return
        scope.launch { submitLogin(email, password) }
    }

    fun register(username: String, email: String, password: String) {
        if (username.isBlank() || email.isBlank() || password.isBlank()) return
        scope.launch { submitRegister(username, email, password) }
    }

    suspend fun submitLogin(email: String, password: String): String? {
        if (email.isBlank() || password.isBlank() || _submitting.value) return null
        return submit { repository.login(email.trim(), password) }
    }

    suspend fun submitRegister(username: String, email: String, password: String): String? {
        if (username.isBlank() || email.isBlank() || password.isBlank() || _submitting.value) return null
        return submit { repository.register(username.trim(), email.trim(), password) }
    }

    companion object {
        const val PASSWORD_MISMATCH_MESSAGE = "两次输入的密码不一致"
        /** Confirmation is a form-layer rule; pure function so it stays JVM-testable. */
        fun passwordsMatch(password: String, confirmation: String): Boolean = password == confirmation
    }
```

- [ ] **Step 3: AppViewModel.register 四参数 + 确认密码守卫**

```kotlin
    fun register(username: String, email: String, password: String, confirmation: String, onResult: (String?) -> Unit) = viewModelScope.launch {
        // confirmation 校验属表单层（spec §3）：不一致即时提示，不经网络
        if (username.isBlank() || email.isBlank() || password.isBlank() || auth.submitting.value) return@launch
        if (!AuthViewModel.passwordsMatch(password, confirmation)) {
            onResult(AuthViewModel.PASSWORD_MISMATCH_MESSAGE)
            return@launch
        }
        onResult(auth.submitRegister(username.trim(), email.trim(), password))
    }
```

（注释「上游 UI 字段名为 email，其值作为后端 username 使用」删除——语义已对齐。）

- [ ] **Step 4: AuthScreen 表单**

LoginScreen 139-146 区域：`message = "请使用注册邮箱联系支持以重置密码。"` → `message = "请手动联系开发者直接修改密码。"`。

RegisterScreen 302-325 区域：

```kotlin
@Composable
internal fun RegisterScreen(viewModel: AppViewModel, nav: ScreenNavigator) {
    var nickname by remember { mutableStateOf("") }
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var confirmation by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    AuthLayout(title = "注册", onBack = nav::popBackStack) { scale ->
        item {
            AuthHintCard("注册完成后请使用邮箱与密码登录。", scale)
        }
        item { AuthField("用户名", "请输入用户名", "badge", nickname, { nickname = it }, false, scale) }
        item { AuthField("邮箱", "请输入邮箱", "alternate_email", email, { email = it }, false, scale) }
        item { AuthField("密码", "至少 8 位", "lock", password, { password = it }, true, scale) }
        item { AuthField("确认密码", "再次输入密码", "lock", confirmation, { confirmation = it }, true, scale) }
        message?.let { text -> item { AuthMessage(text, scale) } }
        item {
            AuthPrimaryButton("完成注册", scale) {
                if (!AuthViewModel.passwordsMatch(password, confirmation)) {
                    message = AuthViewModel.PASSWORD_MISMATCH_MESSAGE
                } else {
                    viewModel.register(nickname, email, password, confirmation) { error ->
                        if (error == null) nav.popBackStack() else message = error
                    }
                }
            }
        }
    }
}
```

（Task 5 会把两处 `AuthMessage(text, scale)` 换成倒计时签名——本步先保持编译一致：若 Task 5 未到，维持现签名。）

- [ ] **Step 5: 更新 JVM 测试**

AuthViewModelTest：所有 `submitRegister(username, password)` / `submitLogin(username, password)` 调用改为三参数/email；新增：

```kotlin
    @Test fun `passwordsMatch is true only for identical passwords`() {
        assertTrue(AuthViewModel.passwordsMatch("secret-pass-1", "secret-pass-1"))
        assertFalse(AuthViewModel.passwordsMatch("secret-pass-1", "secret-pass-2"))
    }

    @Test fun `register forwards username email and password`() = runTest {
        // fake repository 记录 (username, email, password)，断言与调用一致且 username 不被小写化
    }
```

AuthClientContractTest：register/login 请求体断言更新（register body 含 username/email/password 三键；login body 含 email/password 两键、无 username）；fake 会话响应形状不变。

- [ ] **Step 6: 跑 JVM 测试 + assemble**

Run: `cd frontend-app/Front && ./gradlew test && ./gradlew assembleDebug`
Expected: BUILD SUCCESSFUL（53+ 全绿）

- [ ] **Step 7: Commit（frontend-app）**

```bash
git -C /home/kbzz1/shanka_backend/frontend-app add -A && git -C /home/kbzz1/shanka_backend/frontend-app commit -m "feat(auth): 注册三字段 + email 登录接线 + 确认密码校验 + 忘记密码文案"
```

---

### Task 5: 错误映射表 + 3 秒倒计时提示组件

**Files:**
- Create: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/ErrorMessages.kt`
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthViewModel.kt:29-36`（authErrorMessage 改走映射表）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AuthScreen.kt:680-687`（AuthMessage 倒计时）+ 两处调用点（LoginScreen:147、RegisterScreen:316）
- Test: `frontend-app/Front/app/src/test/java/com/qiuzhao/flashcards/ui/auth/ErrorMessagesTest.kt`（新）+ AuthViewModelTest 更新

**Interfaces:**
- Consumes: Task 4 的 AuthViewModel 形状；`ApiResult.Failure(status: Int, code: String?, ...)`（RemoteFlashcards.kt:84）；网络失败 code="NETWORK_UNAVAILABLE"（RemoteFlashcards.kt:267-273 unavailableResult）
- Produces: `ErrorMessages.forCode(code: String?): String`；`AuthMessage(text, scale, onDismiss)` 三参数签名（3 秒倒计时，右侧纯数字【3】【2】【1】，归零整体消失）

- [ ] **Step 1: ErrorMessages.kt（全量映射表）**

```kotlin
package com.qiuzhao.flashcards.ui.auth

/**
 * Full error-code → user-facing message map (spec §4.1): every backend error code has
 * a dedicated Chinese message; unknown codes fall back to [UNKNOWN_ERROR_MESSAGE].
 * Transport failures never reach this table — they are mapped to the network message
 * before lookup (see AuthViewModel.authErrorMessage).
 */
object ErrorMessages {
    const val UNKNOWN_ERROR_MESSAGE = "操作失败，请稍后重试"

    private val byCode: Map<String, String> = mapOf(
        "VALIDATION_ERROR" to "请求参数有误，请检查输入",
        "AUTH_REQUIRED" to "请先登录",
        "AUTH_INVALID" to "登录已失效，请重新登录",
        "INVALID_CREDENTIALS" to "邮箱或密码错误",
        "EMAIL_TAKEN" to "邮箱已被占用",
        "RATE_LIMITED" to "请求过于频繁，请稍后重试",
        "IDEMPOTENCY_CONFLICT" to "请求冲突，请勿重复提交",
        "INTERNAL_ERROR" to "服务器内部错误，请稍后重试",
        "PDF_UPLOAD_INVALID" to "文件不符合要求，请上传有效 PDF",
        "PDF_PARSE_FAILED" to "PDF 解析失败，请换一份文件重试",
        "PDF_TOC_MISSING" to "PDF 缺少目录结构，无法生成",
        "PDF_NOT_FOUND" to "文件不存在或已删除",
        "CHAPTER_NOT_FOUND" to "章节不存在或已删除",
        "API_KEY_UNAVAILABLE" to "AI 服务暂不可用，请稍后重试",
        "API_KEY_NOT_SET" to "请先在设置中配置 API Key",
        "TASK_NOT_FOUND" to "任务不存在或已删除",
        "TASK_STATE_CONFLICT" to "任务状态已变化，请刷新重试",
        "TASK_NOT_RESUMABLE" to "任务无法继续",
        "TASK_IN_PROGRESS" to "资源正被任务使用，暂无法操作",
        "GENERATION_FAILED" to "生成失败，请稍后重试",
        "DECK_NOT_FOUND" to "牌组不存在或已删除",
        "CARD_NOT_FOUND" to "卡片不存在或已删除",
        "GENERATION_ITEM_CONFLICT" to "生成项冲突，请刷新重试",
        "IMPORT_PARSE_ERROR" to "导入内容解析失败，请检查格式",
        "REWRITE_SCHEMA_INVALID" to "改写结果不符合要求，请重试",
        "REVIEW_EVENT_INVALID" to "复习记录无效",
        "REVIEW_EVENT_CONFLICT" to "复习记录冲突，请刷新重试",
    )

    fun forCode(code: String?): String = code?.let { byCode[it] } ?: UNKNOWN_ERROR_MESSAGE
}
```

（文案须与 structure-contract §7 错误码表语义一致；若实现时发现后端某码 message 语义更新，以 spec 语义为准同步本表。）

- [ ] **Step 2: AuthViewModel 映射接线**

替换 29-36 行区域：

```kotlin
const val NETWORK_ERROR_MESSAGE = "网络错误，请稍后重试"

/**
 * Transport failures (no HTTP response at all — BackendClient.unavailableResult produces
 * code NETWORK_UNAVAILABLE) show the network message; every real server error code goes
 * through the full [ErrorMessages] table, unknown codes included (generic fallback).
 */
private fun ApiResult.Failure.authErrorMessage(): String =
    if (code == "NETWORK_UNAVAILABLE") NETWORK_ERROR_MESSAGE else ErrorMessages.forCode(code)
```

- [ ] **Step 3: AuthMessage 倒计时组件**

替换 AuthScreen.kt 680-687 行：

```kotlin
@Composable
private fun AuthMessage(text: String, scale: Float, onDismiss: () -> Unit) {
    var countdown by remember(text) { mutableStateOf(3) }
    LaunchedEffect(text) {
        while (countdown > 0) {
            delay(1_000L)
            countdown -= 1
        }
        onDismiss()
    }
    Surface(
        color = Color(0xFFFFECEA),
        shape = RoundedCornerShape((16 * scale).dp),
        modifier = Modifier.fillMaxWidth()
    ) {
        Row(
            modifier = Modifier.padding((16 * scale).dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            AppText(text, AppTextRole.Supporting, color = Color(0xFF9D2722), designScale = scale)
            Spacer(Modifier.weight(1f))
            AppText("【$countdown】", AppTextRole.Supporting, color = Color(0xFF9D2722), designScale = scale)
        }
    }
}
```

两处调用点更新：LoginScreen `message?.let { AuthMessage(it, scale) }` → `message?.let { AuthMessage(it, scale) { message = null } }`；RegisterScreen 同。语义：3 秒倒计时（【3】→【2】→【1】）归零后**整个提示框消失**（onDismiss 置 message=null）；`remember(text)` 保证新错误重置倒计时。所需 import（`kotlinx.coroutines.delay` 已有；`Row`/`Alignment`/`Spacer` 已在文件 import 中，核对即可）。

- [ ] **Step 4: 测试**

ErrorMessagesTest.kt（新）：

```kotlin
class ErrorMessagesTest {
    @Test fun `every known backend code maps to a non-blank Chinese message`() {
        val codes = listOf(
            "VALIDATION_ERROR", "AUTH_REQUIRED", "AUTH_INVALID", "INVALID_CREDENTIALS",
            "EMAIL_TAKEN", "RATE_LIMITED", "IDEMPOTENCY_CONFLICT", "INTERNAL_ERROR",
            "PDF_UPLOAD_INVALID", "PDF_PARSE_FAILED", "PDF_TOC_MISSING", "PDF_NOT_FOUND",
            "CHAPTER_NOT_FOUND", "API_KEY_UNAVAILABLE", "API_KEY_NOT_SET",
            "TASK_NOT_FOUND", "TASK_STATE_CONFLICT", "TASK_NOT_RESUMABLE", "TASK_IN_PROGRESS",
            "GENERATION_FAILED", "DECK_NOT_FOUND", "CARD_NOT_FOUND", "GENERATION_ITEM_CONFLICT",
            "IMPORT_PARSE_ERROR", "REWRITE_SCHEMA_INVALID", "REVIEW_EVENT_INVALID",
            "REVIEW_EVENT_CONFLICT",
        )
        codes.forEach { code ->
            val message = ErrorMessages.forCode(code)
            assertTrue(message.isNotBlank(), "$code should map to a message")
            assertNotEquals(ErrorMessages.UNKNOWN_ERROR_MESSAGE, message, "$code should not fall through")
        }
    }

    @Test fun `unknown and null codes fall back to the generic message`() {
        assertEquals(ErrorMessages.UNKNOWN_ERROR_MESSAGE, ErrorMessages.forCode("NO_SUCH_CODE"))
        assertEquals(ErrorMessages.UNKNOWN_ERROR_MESSAGE, ErrorMessages.forCode(null))
    }
}
```

AuthViewModelTest 更新：失败映射断言——`Failure(status=400, code="VALIDATION_ERROR", ...)` → 「请求参数有误，请检查输入」（不再是网络错误）；`code="NETWORK_UNAVAILABLE"` → 「网络错误，请稍后重试」；未知码 → 「操作失败，请稍后重试」。

- [ ] **Step 5: 跑 JVM 测试**

Run: `cd frontend-app/Front && ./gradlew test`
Expected: BUILD SUCCESSFUL

- [ ] **Step 6: Commit（frontend-app）**

```bash
git -C /home/kbzz1/shanka_backend/frontend-app add -A && git -C /home/kbzz1/shanka_backend/frontend-app commit -m "feat(auth): 全量错误码映射表 + 3 秒倒计时错误提示（【3】【2】【1】归零消失）"
```

---

### Task 6: 平台契约适配 + 429 重试 + PLANNING 3 组预算

**Files:**
- Modify: `test-platform/shanka/client.py:68-88`（register/login 签名）
- Modify: `test-platform/shanka/account.py`（bootstrap 三参数 + 429 重试 + temp_email）
- Modify: `test-platform/shanka/environments.py:34-41`（credentials 返回 email）
- Modify: `test-platform/shanka/cost.py:44,80-95,109`（planning_groups 参数）
- Modify: `test-platform/scenarios/auth/auth.py:48,53`、`scenarios/isolation/isolation.py:78,104,193`、`scenarios/baseline/api_smoke.py:103,197`、`scenarios/flow/live_flow.py:101,255,298`、`runner/suites.py:57`（调用点适配）
- Modify: `test-platform/CLAUDE.md`（前提声明句 + 场景地图无变化）
- Test: `test-platform/tests/test_account.py`（新）、`test-platform/tests/test_cost.py`（更新）

**Interfaces:**
- Consumes: 后端新契约（Task 2）；`Response(headers=小写键)`（client.py:41-46）；`environments.credentials()` 现返回 tuple[str, str]
- Produces: `client.register(username, email, password)` / `client.login(email, password)`；`account.bootstrap(c, *, environment, username, email, password)`；`account.temp_email(run_id, tag)`；`credentials() -> tuple[str, str, str]`（新增环境变量 `SHANKA_TEST_EMAIL`）；`derive_budget(*, chapters, quantity_tendency, generate, planning_groups=1)`

- [ ] **Step 1: client.py 签名切换**

```python
    def register(self, username: str, email: str, password: str) -> Response:
        """POST /auth/register:恒不带头/不重试/不落事件(凭据与响应 token 脱敏)。

        Authorization 显式剥离(即使先 set_token 也不发送)——brief 硬性语义,
        不依赖后端对 /auth/register 的鉴权豁免。
        """
        return self._credential_request(
            "/auth/register", {"username": username, "email": email, "password": password}
        )

    def login(self, email: str, password: str) -> Response:
        """POST /auth/login:恒不带头/不重试/不落事件。token 由调用方按需 set_token 持有。"""
        return self._credential_request("/auth/login", {"email": email, "password": password})

    def _credential_request(self, path: str, body: dict) -> Response:
        return self.request("POST", path, body=body, retry=False, auth=False)
```

- [ ] **Step 2: account.py bootstrap 429 重试 + temp_email**

```python
from collections.abc import Callable
import time

_MAX_429_RETRIES = 3


def _retry_429(call: Callable[[], Response], what: str) -> Response:
    """429 显式重试(限 3 次):429 是服务端业务执行前的明确拒绝(限流桶检查先于
    建用户/建会话),重试无重放副作用;FR-19 防的是超时/5xx 未知结果重放,不冲突。
    """
    r = call()
    for attempt in range(_MAX_429_RETRIES):
        if r.status != 429:
            return r
        try:
            wait = int(r.headers.get("retry-after", "2")) + 1  # headers 键已小写(client.py)
        except (TypeError, ValueError):
            wait = 2
        print(f"    [429] {what} 限流,等待 {wait}s 后重试({attempt + 1}/{_MAX_429_RETRIES})")
        time.sleep(wait)
        r = call()
    return r


def bootstrap(
    c: ShankaClient,
    *,
    environment: str,
    username: str,
    email: str,
    password: str,
) -> dict | None:
    """按环境注册或登录并 set_token;成功返回会话信息(含 created_local_user),失败返回 None。

    local: 先 register,201/200 即新建本地用户行(created_local_user=True);
           409 EMAIL_TAKEN(账号已存在)回落 login;429 按 Retry-After 重试 ≤3 次;
           仍 429 打印明确指引后返回 None;其他失败不静默回落。
    prod: 只 login,不自动注册;429 同样重试。
    """
    if auth_mode(environment) == "register":
        r = _retry_429(lambda: c.register(username, email, password), "register")
        if r.status in _SESSION_STATUS:
            session = parse_session(r)
            if session:
                c.set_token(session["access_token"])
                return {**session, "created_local_user": True}
        if r.status == 429:
            print(f"    [FAIL] register 限流重试 {_MAX_429_RETRIES} 次后仍 429,请等待限流窗口(1 小时)后重跑")
            return None
        if r.status != 409:
            return None
    r = _retry_429(lambda: c.login(email, password), "login")
    if r.status == 429:
        print(f"    [FAIL] login 限流重试 {_MAX_429_RETRIES} 次后仍 429,请等待限流窗口(1 小时)后重跑")
        return None
    if r.status not in _SESSION_STATUS:
        return None
    session = parse_session(r)
    if session is None:
        return None
    c.set_token(session["access_token"])
    return {**session, "created_local_user": False}


def temp_email(run_id: str, tag: str) -> str:
    """run_id 派生的本地临时测试邮箱(与 temp_username 对应,不落日志)。"""
    return f"{temp_username(run_id, tag)}@local.test"
```

模块 docstring「local 先 register(409 USERNAME_TAKEN 回落 login)」→「local 先 register(409 EMAIL_TAKEN 回落 login)」。

- [ ] **Step 3: environments.credentials() 加 email**

```python
EMAIL_ENV = "SHANKA_TEST_EMAIL"


def credentials() -> tuple[str, str, str]:
    """读取测试账号凭据;缺失时抛 MissingCredentialsError(调用方决定退出码)。"""
    username = os.environ.get(USERNAME_ENV, "")
    email = os.environ.get(EMAIL_ENV, "")
    password = os.environ.get(PASSWORD_ENV, "")
    missing = [
        name
        for name, value in ((USERNAME_ENV, username), (EMAIL_ENV, email), (PASSWORD_ENV, password))
        if not value
    ]
    if missing:
        raise MissingCredentialsError(f"缺少测试账号凭据环境变量: {', '.join(missing)}(不自动注册)")
    return username, email, password
```

- [ ] **Step 4: 调用点适配**

- `scenarios/auth/auth.py`：`run(c, *, environment, username, email, password)`；`c.login(account.wrong_password(password))` → `c.login(email, account.wrong_password(password))`；`account.bootstrap(c, environment=environment, username=username, email=email, password=password)`；main() 解包三值 `username, email, password = environments.credentials()`。
- `scenarios/isolation/isolation.py`：两处 bootstrap 加 email（第二账号用 `account.temp_email(run_id, tag2)`）；credentials 解包。
- `scenarios/baseline/api_smoke.py`、`scenarios/flow/live_flow.py`：bootstrap 调用加 email；credentials 解包。
- `runner/suites.py:57`：credentials() 解包改三值（email 透传给场景 run）。
- 全部 `environments.credentials()` 解包点 grep 核对（5 处）；场景 main() 的 argparse 不需加 --email（凭据只走 env）。

- [ ] **Step 5: cost.py PLANNING 组数**

```python
def derive_budget(
    *, chapters: int, quantity_tendency: str, generate: bool, planning_groups: int = 1
) -> Budget:
    # PLANNING 组数前提(V2.4 fixture 锚定):样书前 2 章 42.6k 字符 ÷ planner_max_input_chars
    # 20k(config.py)= 3 组向上取整;组数由 fixture 显式声明(planning_groups),
    # 实际组数受后端 max_planner_groups_per_task=30 上限;调整样书或阈值需同步声明
    planning = planning_groups * (1 + _PLANNING_RETRY_LIMIT) if generate else 0
```

Budget dataclass 的 `planning_calls` 注释同步（「1 规划组 × (1+重试上限)」→「planning_groups 规划组 × (1+重试上限)」）；`BUDGET_FIXTURE`（live_flow.py 或 cost.py 中定义处）加 `"planning_groups": 3`；budget 打印行（cost.py:115 附近）加 `PLANNING×{planning_groups}组`；test-platform/CLAUDE.md「成本与环境闸门」节前提声明句同步（「PLANNING 按 1 规划组计——前提：前 2 章累计页文本 ≤ 20k…」改为「PLANNING 按 fixture 声明 3 规划组计（前 2 章 42.6k 字符 ÷ 20k 向上取整）」）；`docs/superpowers/specs/2026-08-12-test-platform-design.md` 相应句末尾追加勘误注（不改写历史正文）。

- [ ] **Step 6: 平台单测**

test_account.py（新，stdlib unittest 风格；用假 Response + 假 client 不触网）：

```python
class Bootstrap429RetryTest(unittest.TestCase):
    def test_register_429_then_201_retries_and_succeeds(self):
        # 假 client：第一次 register 返回 429(headers={"retry-after": "1"})，
        # 第二次返回 201 会话形状 → bootstrap 返回会话且 created_local_user=True
        # （time.sleep 用 unittest.mock.patch 替掉，断言 sleep 被调用）
    def test_login_429_three_times_then_fails_with_none(self):
        # 三次 login 都 429 → bootstrap 返回 None（print 指引不抛异常）
    def test_register_409_falls_back_to_login_with_email(self):
        # register 409 → 回落 login(email)，断言 login 收到的 email
```

test_cost.py 更新：`derive_budget(chapters=2, quantity_tendency="...", generate=True, planning_groups=3)` → `budget.planning_calls == 9`；默认 `planning_groups=1` → 3。

- [ ] **Step 7: 跑平台测试**

Run: `cd test-platform && conda run -n shanka-backend python -m pytest tests/ -q`
Expected: 全 PASS（82+ 新增）

- [ ] **Step 8: Commit**

```bash
git add test-platform/ docs/superpowers/specs/2026-08-12-test-platform-design.md
git commit -m "feat(test-platform): email 契约适配 + bootstrap 429 重试 + PLANNING 3 组预算前提"
```

---

### Task 7: 契约同步（PRD V2.4 / structure-contract / Progress）

**Files:**
- Create: `docs/PRD/V2.4/prd_v2_4.md`
- Modify: `docs/Architecture/structure-contract.md`（3.14/3.15/6.11/7/1.6）
- Modify: `docs/Progress.md`（追加行）
- 核对：`docs/PRD/AGENTS.md`（版本清单若有需登记）、`docs/AGENTS.md`（需求权威指向 V2.4）

**Interfaces:**
- Consumes: Task 1/2/3 的最终实现形态（迁移 revision id、错误码、文案、滑动续期语义）

- [ ] **Step 1: PRD V2.4**

新建 `docs/PRD/V2.4/prd_v2_4.md`（继承 V2.3，结构参照 prd_v2_3.md 的「继承 + 变更清单」模式），变更清单：

1. 账号登录键切换：login 用 email+password；register 收 username+email+password。
2. username 降为展示名：1-24 位中文/字母/数字/._-，可重名（去 UNIQUE），不再强制小写；主界面展示用。
3. users 表加 email 列（NOT NULL + UNIQUE，lowercase 规范化）；迁移清空存量账号及下游数据（决策记录：开发期数据清空重来）；downgrade 拒绝。
4. 错误码：EMAIL_TAKEN(409) 新增；USERNAME_TAKEN 移除；INVALID_CREDENTIALS 文案「邮箱或密码错误」。
5. 长期登录：会话滑动续期（活跃续期至 now+30 天，按天节流；连续 30 天不活跃过期；登出即时失效）。
6. 前端错误提示 UX：全量错误码→中文文案映射 + 3 秒倒计时（右侧纯数字【3】【2】【1】）归零整体消失；仅登录/注册屏。
7. 忘记密码提示文案「请手动联系开发者直接修改密码。」；注册确认密码本地校验；密码提示「至少 8 位」。
8. 平台：bootstrap 429 按 Retry-After 重试 ≤3 次；PLANNING 预算按 3 规划组推导。

排除项：登录屏「直接进入」按钮维持现状（用户裁决不动）。

- [ ] **Step 2: structure-contract.md 四处**

- 3.14 AuthUser：username 行改为「`username` | string | ✓ | 1~24 位展示名，中文/字母/数字/._-，可重名」；规则句删「全库唯一/转小写」表述。
- 3.15 AuthSessionResponse：expires_at 说明追加「；V2.4 起活跃滑动续期（每次有效请求后剩余不足 1 天则续至 +30 天，按天节流）」。
- 6.11 接口表：register 行改「创建用户并建立会话;`{ username, email, password }`;201 返回 AuthSessionResponse(3.15);email 冲突 → `409 EMAIL_TAKEN`」；login 行改「校验凭据并建立新会话;`{ email, password }`;200 返回 AuthSessionResponse;失败统一 `401 INVALID_CREDENTIALS`(邮箱或密码错误)」；规则段「login 的非法格式用户名按输入校验惯例返回 400」→「login 的非法格式邮箱」。
- 7 错误码表：`USERNAME_TAKEN` 行换为「`EMAIL_TAKEN` | 409 | 注册邮箱已被占用」；`INVALID_CREDENTIALS` 说明「用户名不存在与密码错误」→「邮箱不存在与密码错误」。
- 1.6 限流策略表：「登录(用户名分桶) | 按规范化用户名」→「登录(邮箱分桶) | 按规范化邮箱」。

- [ ] **Step 3: Progress.md + 文档清单**

Progress.md 按惯例追加本工作包一行（日期 + 状态图例，参照 T3 先例格式）；docs/AGENTS.md 需求权威句若写死 V2.2/V2.3 → 更新为 V2.4。docs/PRD/ 下 AGENTS.md 若有版本清单 → 登记 V2.4。

- [ ] **Step 4: 全契约 grep 校验**

```bash
grep -rn "USERNAME_TAKEN" docs/Architecture/ docs/PRD/V2.4/ main/app/ main/services/ main/tests/ 2>/dev/null
```
Expected: 仅历史迁移文件注释（ddc6f34e30b8）与 PRD V2.2/V2.3 历史文档命中；运行时/契约层零命中。`grep -rn "login_username" main/` → 零命中。

- [ ] **Step 5: 守卫与后端全测**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_orm_database_guard.py tests/contract -q 2>/dev/null || conda run -n shanka-backend python -m pytest -q`
Expected: 全 PASS（契约守卫含 openapi↔schemas 一致性测试若有则必须绿）

- [ ] **Step 6: Commit**

```bash
git add docs/PRD/ docs/Architecture/structure-contract.md docs/Progress.md docs/AGENTS.md
git commit -m "docs: PRD V2.4 + structure-contract 契约同步（email 登录/滑动续期/错误码）"
```

---

### Task 8: instrumented 更新 + 三面全量回归 + 收尾

**Files:**
- Modify: `frontend-app/Front/app/src/androidTest/java/com/qiuzhao/flashcards/data/remote/AuthFlowInstrumentedTest.kt:59-90`
- Modify: `frontend-app/Front/app/src/androidTest/java/com/qiuzhao/flashcards/data/remote/BackendClientInstrumentedTest.kt`（若含 auth 端点请求体断言）
- Modify: `docs/superpowers/specs/2026-08-14-email-login-and-error-ux-design.md`（§8 验收勾选）
- Modify: `docs/superpowers/plans/2026-08-14-email-login-and-error-ux.md`（本文件勾选）

**Interfaces:**
- Consumes: Task 4/5 的 AuthViewModel/AuthRepository 新签名；真机通道：Windows adb server（C:\Users\kbzz1\.cache\platform-tools）+ `ADB_SERVER_SOCKET=tcp:127.0.0.1:5037`

- [ ] **Step 1: AuthFlowInstrumentedTest 改 email 语义**

```kotlin
    @Test fun fullAuthFlowRegisterLoginLogoutRelogin() = runBlocking {
        ...
        val username = "真机测试用户"  // 展示名：中文合法（V2.4 放宽）
        val email = "t12-${UUID.randomUUID().toString().take(8)}@local.test"
        val password = "test-pass-1"
        assertNull(auth.submitRegister(username, email, password))
        assertEquals(username, (auth.state.value as AuthState.LoggedIn).user.username)
        auth.logout()
        assertTrue(auth.state.value is AuthState.LoggedOut)
        assertNull(auth.submitLogin(email, password))
        assertEquals(username, (auth.state.value as AuthState.LoggedIn).user.username)
        ...
    }
```

（原名/断言结构保留，仅字段与中文展示名按 V2.4 语义更新；instrumentation argument 注入 baseUrl 的既有机制不变。）

- [ ] **Step 2: 三面全量回归（非真机部分）**

```bash
cd main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .
cd ../test-platform && conda run -n shanka-backend python -m pytest tests/ -q
cd ../frontend-app/Front && ./gradlew test --rerun-tasks && ./gradlew assembleDebug && ./gradlew assembleDebugAndroidTest
```
Expected: 后端 565+ 全绿（数量随改写变化）/ ruff/format/mypy 全绿；平台 82+ 全绿；gradle 53+ 全绿 + 双 assemble。

- [ ] **Step 3: 真机 connectedDebugAndroidTest**

- 通道复用：确认 Windows adb server 在位（`powershell.exe -NoProfile -Command "C:\Users\kbzz1\.cache\platform-tools\adb.exe devices"` 见设备）；WSL 内 `ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 /home/kbzz1/android-sdk/platform-tools/adb devices`。
- 测试前保持唤醒（T14 毛刺教训）：`adb shell svc power stayon true`；跑完恢复 `adb shell svc power stayon false`。
- Run: `cd frontend-app/Front && ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 ./gradlew connectedDebugAndroidTest --console=plain`
- Expected: 18/18（BackendClientInstrumentedTest 13 + AuthFlowInstrumentedTest 3 + FlashcardsAppTest 1 + StoredSessionEntersMainScreenTest 1），XML failures=0。
- fullAuthFlow 触网线上 shanka.kbzz1.top（本机 uvicorn 127.0.0.1:8000 在跑是源站，不要停；429 等 1-2 分钟重试）。

- [ ] **Step 4: spec §8 验收勾选 + 计划勾选 + 提交**

spec `2026-08-14-email-login-and-error-ux-design.md` §8 十条 AC 按证据勾选 `- [x]`（AC-01~AC-10 逐条对应 Task 1/2/3/4/5/6/7 证据 + 本任务回归证据）；本计划文件任务复选框勾选；两仓库提交：

```bash
git add docs/superpowers/specs/2026-08-14-email-login-and-error-ux-design.md docs/superpowers/plans/2026-08-14-email-login-and-error-ux.md
git commit -m "docs: 验收总览勾选——email 登录 + 错误提示 UX + 长期登录工作包完成"
git -C /home/kbzz1/shanka_backend/frontend-app add -A && git -C /home/kbzz1/shanka_backend/frontend-app commit -m "test(androidTest): 登录链路 email 语义更新（V2.4）"
```

- [ ] **Step 5: 报告**

SDD workspace `.superpowers/sdd/2026-08-14-email-login-and-error-ux/` 下 task-8-report.md 汇总三面回归证据、真机 18/18 XML 路径、AC 勾选依据、concerns。

---

## 执行顺序

```
T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8（串行）
```

- T4/T5 依赖 T2（契约形状）；T6 依赖 T2；T7 依赖 T1/T2/T3（实现终态）；T8 依赖全部。
- T4 与 T5 同属 frontend-app，串行避免嵌套仓库提交冲突。
- 真机测试在 T8（复用 T14 建立的 Windows adb server 通道）。
