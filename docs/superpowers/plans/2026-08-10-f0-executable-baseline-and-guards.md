# F0 可执行基线与防漂移护栏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**结果**：<主 Agent 整包验收通过后在此注明 F0 DONE 与证据位置>

**Goal:** 建立可运行、可安装、可验证的后端基线与四道防漂移护栏：Settings 唯一入口、DB session 唯一入口、统一错误对象与错误码注册表、healthz/readyz 探针、四类契约守卫与测试基座，解决 R-01/R-02，使 F0 依据真实验收证据标记 DONE。

**Architecture:** 契约驱动分层，`app → services → infra` 单向。F0 只建立跨包共享的地基：`app/config.py`（配置唯一入口）、`infra/db/session.py`（DB 唯一入口 + 统一时间格式）、`infra/clock.py`（时钟唯一入口）、`app/errors.py`（错误码唯一注册位置 + localization_key 派生规则，解决 R-01）、`app/schemas/common.py`（错误响应 schema）、`app/api/probes.py`（healthz/readyz）、`app/middleware/error_handler.py`（AppError → 1.4 错误响应）、`infra/storage/local.py`（就绪检查用可写性探测）。契约守卫全部落在 `tests/contract/`，以 `docs/Architecture/` 与 `agent_evolution/manifest.json` 为权威来源解析比对，不建立第二套错误码权威。

**Tech Stack:** Python 3.12、FastAPI、pydantic-settings、SQLAlchemy 2.0（仅 engine 层，无 ORM 模型）、pytest、ruff、mypy strict、hatchling（build backend）、pip-tools（锁定，解决 R-02）。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行，禁止向 base/系统 Python 装依赖（CLAUDE.md 工具链）。
- 实现不得修改 `docs/PRD/`、`docs/Architecture/`；本计划仅 `main/` 与仓库根 `.gitignore`、`docs/Progress.md`（仅主 Agent 在 Task 8/9）。
- 错误响应必须符合 structure-contract 1.4：`{"error": {"code", "message", "localization_key"}}` 三字段全必填。
- 错误码 ↔ HTTP 状态 ↔ structure-contract 第 7 章必须逐一一致（23 个错误码，见 Task 4）。
- localization_key 派生规则（R-01 解决）：`"error." + 错误码.lower()`；`app/errors.py` 的 `LOCALIZATION_KEYS` 是唯一文案清单，守卫校验派生集合与清单全等。
- 时间格式唯一规范（database-design §0）：`YYYY-MM-DDTHH:MM:SS.sssZ`（UTC、恒 3 位毫秒），由 `format_utc` 生成；naive datetime 抛 ValueError。
- DB 连接配置（database-design §0）：`PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON` 在 engine 级 connect 事件统一配置；SQLite 必须 `check_same_thread=False`；写事务 `BEGIN IMMEDIATE` 由 F1 接入并验证（F0 不引入未验证配置）。
- API Key 明文（`DEEPSEEK_API_KEY`）禁止打印/复制/写入日志、fixture、测试报告；Settings 中该字段 `repr=False`。
- ruff line-length 100、mypy strict；四个工具命令全绿才算完成：`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`（均在 `main/` 下）。
- 测试命名 `test_<模块>_<行为>`；unit 不触 DB/网络/文件；integration 走真实临时 SQLite/文件系统；contract 解析正式文档。
- 提交只 `git add` 本任务文件，禁止卷入工作区既有未提交改动（AGENTS.md、docs/Progress.md、docs/superpowers/plans/AGENTS.md、.gitignore）。
- 工作包边界：F0 不含 ORM 模型/迁移（F1）、中间件鉴权/幂等/限流/request_id（F1）、metrics（F1）、Alembic（F1）、存储 PDF 管理（V3A）。
- Task 1~7 由实现 subagent 完成；Task 8/9 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: 可编辑安装与依赖锁定（R-02）

**Files:**
- Modify: `main/pyproject.toml`（整体重写）
- Create: `main/requirements-dev.lock`（生成产物，提交）
- Modify: `.gitignore`（追加运行时产物条目）

**Interfaces:**
- Consumes: 无（本任务为全仓依赖地基）
- Produces: hatchling 构建后端；依赖 `sqlalchemy>=2.0`（运行时）、`httpx>=0.27`、`pyyaml>=6.0`、`types-PyYAML>=6.0`（dev）；pytest `pythonpath=["."]`；锁定文件与再生成命令

- [ ] **Step 1: 重写 `main/pyproject.toml`**

```toml
# 依赖与 lint 配置唯一事实源（CLAUDE.md 工具链）
# 锁定方式（R-02 解决）：cd main && conda run -n shanka-backend pip-compile \
#   pyproject.toml --extra dev --output-file requirements-dev.lock
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "shanka-backend"
version = "0.1.0"
description = "闪卡 App v2.1 后端（契约驱动实现）"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.2",
  "sqlalchemy>=2.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "ruff>=0.6",
  "mypy>=1.11",
  "pre-commit>=3.7",
  "httpx>=0.27",
  "pyyaml>=6.0",
  "types-PyYAML>=6.0",
]

[tool.hatch.build.targets.wheel]
packages = ["app", "domain", "services", "infra"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = true
```

- [ ] **Step 2: 追加 `.gitignore` 运行时产物条目**（保留既有 `/res/*.pdf`、`/.env`）

```
# 运行时产物（F0 起）
/main/shanka.db*
/main/storage/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.mypy_cache/
```

- [ ] **Step 3: 可编辑安装并安装锁定工具**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend pip install -e ".[dev]"`
Expected: 成功（不再因多顶层包报错）；随后 `conda run -n shanka-backend pip install pip-tools`
验证: `conda run -n shanka-backend python -c "import app, domain, services, infra, sqlalchemy, yaml, fastapi.testclient"` 无 ImportError

- [ ] **Step 4: 生成锁定文件**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend pip-compile pyproject.toml --extra dev --output-file requirements-dev.lock`
Expected: 生成 `requirements-dev.lock`，含全部运行时与 dev 依赖的钉版本；确认 `fastapi`、`sqlalchemy`、`pytest`、`pyyaml`、`types-PyYAML` 在列

- [ ] **Step 5: 干净环境安装冒烟（锁定结果可复现）**

Run:
```bash
conda run -n shanka-backend python -m venv /tmp/f0-clean-venv
/tmp/f0-clean-venv/bin/pip install -r /home/kbzz1/shanka_backend/main/requirements-dev.lock
/tmp/f0-clean-venv/bin/pip install -e /home/kbzz1/shanka_backend/main
cd /home/kbzz1/shanka_backend/main && /tmp/f0-clean-venv/bin/python -c "import sqlalchemy; print(sqlalchemy.__version__)"
```
Expected: 全部成功；删除冒烟 venv：`rm -rf /tmp/f0-clean-venv`

- [ ] **Step 6: 提交**

```bash
git add main/pyproject.toml main/requirements-dev.lock .gitignore
git commit -m "build: hatchling 可编辑安装 + pip-tools 锁定（R-02 解决）"
```

---

### Task 2: 单一 Settings 配置入口

**Files:**
- Create: `main/app/config.py`
- Test: `main/tests/unit/test_settings.py`

**Interfaces:**
- Consumes: Task 1 的 pydantic-settings 依赖
- Produces: `app.config.Settings`（字段：`app_name: str`、`version: str`、`environment: Literal[development/test/production]`、`log_level: str`、`database_url: str`、`storage_path: Path`、`deepseek_api_key: str | None`（`repr=False`））；可 `Settings(database_url=..., storage_path=...)` 显式构造，默认值进代码

- [ ] **Step 1: 写失败测试 `main/tests/unit/test_settings.py`**

```python
"""app.config 单一配置入口单元测试。"""

from pathlib import Path

from app.config import Settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.app_name == "shanka-backend"
    assert settings.version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_url == "sqlite:///./shanka.db"
    assert settings.storage_path == Path("./storage")
    assert settings.deepseek_api_key is None


def test_settings_env_var_override(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert Settings().database_url == "sqlite:///:memory:"


def test_settings_explicit_kwargs_win_over_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert Settings(database_url="sqlite:///./explicit.db").database_url == "sqlite:///./explicit.db"


def test_settings_secret_hidden_in_repr() -> None:
    settings = Settings(deepseek_api_key="sk-super-secret-value")
    assert "sk-super-secret-value" not in repr(settings)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_settings.py -v`
Expected: FAIL（ModuleNotFoundError: app.config）

- [ ] **Step 3: 实现 `main/app/config.py`**

```python
"""单一配置入口（project-structure 6）：pydantic-settings 单层配置类。

规则：默认值进代码；密钥/令牌走环境变量；禁止散落硬编码。
敏感项清单（不得写入日志、响应、任务明细或测试报告）：
- `DEEPSEEK_API_KEY` → `deepseek_api_key`（`repr=False`，仅 infra/llm 调用路径可读取）

运行位置约定：开发/验收在 `main/` 下运行（env_file=".env" 相对工作目录，
仓库根 .env 不存在于 main/，故测试不会意外加载）；测试一律显式传参构造。
"""

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "shanka-backend"
    version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./shanka.db"
    storage_path: Path = Path("./storage")
    # 敏感项：禁止打印、复制、写入日志/响应/任务明细；`repr=False` 防意外入日志
    deepseek_api_key: str | None = Field(default=None, repr=False)
```

- [ ] **Step 4: 运行确认通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_settings.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/config.py tests/unit/test_settings.py`
Expected: PASS 且 ruff/mypy 无报错

- [ ] **Step 5: 提交**

```bash
git add main/app/config.py main/tests/unit/test_settings.py
git commit -m "feat(config): 单一 Settings 配置入口（默认值进代码、密钥 repr=False）"
```

---

### Task 3: DB session 唯一入口与时间/时钟唯一入口

**Files:**
- Create: `main/infra/db/session.py`
- Create: `main/infra/clock.py`
- Test: `main/tests/unit/test_session_format_utc.py`、`main/tests/unit/test_clock.py`、`main/tests/integration/test_session_engine.py`

**Interfaces:**
- Consumes: 无（engine 接收数据库 URL 字符串，不依赖 Settings）
- Produces: `infra.db.session.create_db_engine(database_url: str) -> Engine`、`infra.db.session.format_utc(dt: datetime) -> str`；`infra.clock.Clock`（Protocol）、`infra.clock.SystemClock`、`infra.clock.FrozenClock`（`now_utc() -> datetime`）；后续任务/服务按 `now_utc()` 获取服务端权威时间

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_session_format_utc.py`**

```python
"""infra.db.session.format_utc 统一时间格式单元测试（database-design 0）。"""

from datetime import UTC, datetime

import pytest

from infra.db.session import format_utc


def test_format_utc_fixed_3ms_and_z_suffix() -> None:
    dt = datetime(2026, 8, 10, 9, 0, 0, 123456, tzinfo=UTC)
    assert format_utc(dt) == "2026-08-10T09:00:00.123Z"


def test_format_utc_truncates_microseconds_to_ms() -> None:
    dt = datetime(2026, 8, 10, 9, 0, 0, 999999, tzinfo=UTC)
    assert format_utc(dt) == "2026-08-10T09:00:00.999Z"


def test_format_utc_converts_offset_to_utc() -> None:
    from datetime import timedelta, timezone

    dt = datetime(2026, 8, 10, 17, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert format_utc(dt) == "2026-08-10T09:00:00.000Z"


def test_format_utc_naive_raises() -> None:
    with pytest.raises(ValueError, match="naive"):
        format_utc(datetime(2026, 8, 10, 9, 0, 0))
```

- [ ] **Step 2: 写失败单元测试 `main/tests/unit/test_clock.py`**

```python
"""infra.clock 时钟唯一入口单元测试（Progress 2.5：时钟唯一入口）。"""

from datetime import UTC, datetime

from infra.clock import FrozenClock, SystemClock


def test_clock_system_now_utc_aware() -> None:
    now = SystemClock().now_utc()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


def test_clock_frozen_returns_fixed_value() -> None:
    fixed = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
    assert FrozenClock(fixed).now_utc() == fixed
```

- [ ] **Step 3: 写失败集成测试 `main/tests/integration/test_session_engine.py`**

```python
"""infra.db.session 引擎集成测试：空测试库创建、WAL/外键（database-design 0）。"""

from pathlib import Path

from sqlalchemy import text

from infra.db.session import create_db_engine


def test_session_engine_creates_empty_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
        tables = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    assert db_path.exists()
    assert tables == []


def test_session_engine_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
```

- [ ] **Step 4: 运行确认三个测试均失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_session_format_utc.py tests/unit/test_clock.py tests/integration/test_session_engine.py -v`
Expected: FAIL（ModuleNotFoundError: infra.db.session / infra.clock）

- [ ] **Step 5: 实现 `main/infra/db/session.py`**

```python
"""DB 唯一入口（database-design 0 / Progress 2.5）。

连接配置（database-design 0，审核修复）：
- `PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;` 在 engine 级 connect 事件统一配置；
- SQLite 必须 `check_same_thread=False`（FastAPI 线程池复用连接）；
- 写事务 `BEGIN IMMEDIATE`（isolation_level='IMMEDIATE'）随 F1 写事务接入并验证，F0 不引入未验证配置。

时间格式唯一规范（database-design 0）：`YYYY-MM-DDTHH:MM:SS.sssZ`
（UTC、零填充、恒 3 位毫秒），由 `format_utc` 统一生成，禁止 `isoformat()` 默认输出。
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, create_engine, event


def _configure_connection(dbapi_connection: Any, connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if database_url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_connection)
    return engine


def format_utc(dt: datetime) -> str:
    """统一 UTC 时间格式（database-design 0）；naive datetime 拒绝（防本地时区陷阱）。"""
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不可序列化：必须携带 UTC 时区")
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt_utc.microsecond // 1000:03d}Z"
```

- [ ] **Step 6: 实现 `main/infra/clock.py`**

```python
"""时钟唯一入口（Progress 2.5）：服务端为权威时钟（structure-contract 1.2）。"""

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock:
    """测试用可控时钟（F0 测试基座）。"""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now_utc(self) -> datetime:
        return self._now
```

- [ ] **Step 7: 运行确认通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_session_format_utc.py tests/unit/test_clock.py tests/integration/test_session_engine.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/config.py app/__init__.py app/main.py domain/ services/ infra/ tests/`
Expected: PASS 且 ruff/mypy 无报错（`app/main.py` 现为占位 docstring，mypy 应通过）

- [ ] **Step 8: 提交**

```bash
git add main/infra/db/session.py main/infra/clock.py main/tests/unit/test_session_format_utc.py main/tests/unit/test_clock.py main/tests/integration/test_session_engine.py
git commit -m "feat(infra): DB session 唯一入口（WAL/外键）+ 统一时间格式 + 时钟唯一入口"
```

---

### Task 4: 统一错误对象与错误码注册表（R-01）

**Files:**
- Create: `main/app/errors.py`
- Test: `main/tests/unit/test_errors.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces: `app.errors.ErrorCode`（23 个成员，str 值即契约错误码）、`app.errors.ERROR_HTTP_STATUS: dict[ErrorCode, int]`、`app.errors.LOCALIZATION_KEYS: frozenset[str]`（唯一文案清单）、`app.errors.http_status(code: ErrorCode) -> int`、`app.errors.localization_key(code: ErrorCode) -> str`、`app.errors.AppError`（`code`/`message` 属性 + `to_response() -> dict`）；Task 5/6/7 与后续全部纵向包使用

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_errors.py`**

```python
"""app.errors 统一错误对象单元测试（structure-contract 1.4 / 7）。"""

import re

from app.errors import (
    ERROR_HTTP_STATUS,
    LOCALIZATION_KEYS,
    AppError,
    ErrorCode,
    http_status,
    localization_key,
)


def test_errors_http_status_covers_all_codes() -> None:
    for code in ErrorCode:
        assert code in ERROR_HTTP_STATUS
        assert 100 <= ERROR_HTTP_STATUS[code] <= 599


def test_errors_http_status_values_match_contract() -> None:
    assert http_status(ErrorCode.DECK_NOT_FOUND) == 404
    assert http_status(ErrorCode.IDEMPOTENCY_CONFLICT) == 409
    assert http_status(ErrorCode.RATE_LIMITED) == 429
    assert http_status(ErrorCode.API_KEY_UNAVAILABLE) == 502


def test_errors_localization_key_derivation() -> None:
    assert localization_key(ErrorCode.DECK_NOT_FOUND) == "error.deck_not_found"
    assert localization_key(ErrorCode.PDF_PARSE_FAILED) == "error.pdf_parse_failed"


def test_errors_localization_keys_explicit_list_matches_derived() -> None:
    derived = frozenset(localization_key(code) for code in ErrorCode)
    assert derived == LOCALIZATION_KEYS


def test_errors_localization_key_format() -> None:
    for key in LOCALIZATION_KEYS:
        assert re.fullmatch(r"error\.[a-z0-9_]+", key)


def test_errors_app_error_response_shape() -> None:
    err = AppError(ErrorCode.DECK_NOT_FOUND, "未找到牌组")
    assert err.code is ErrorCode.DECK_NOT_FOUND
    assert err.message == "未找到牌组"
    assert err.to_response() == {
        "error": {
            "code": "DECK_NOT_FOUND",
            "message": "未找到牌组",
            "localization_key": "error.deck_not_found",
        }
    }
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_errors.py -v`
Expected: FAIL（ModuleNotFoundError: app.errors）

- [ ] **Step 3: 实现 `main/app/errors.py`**

```python
"""统一错误对象与错误码注册表（structure-contract 1.4 / 7；红线 3：格式统一于 app/middleware）。

R-01 解决（唯一位置与派生规则）：
- 错误码唯一注册位置 = 本模块 `ErrorCode`；
- `localization_key` 由错误码派生：`error.` + 错误码 snake_case（如 DECK_NOT_FOUND → error.deck_not_found）；
- 文案清单唯一位置 = `LOCALIZATION_KEYS` 显式集合；契约守卫校验派生集合与清单全等，不另建文件。
错误码 ↔ HTTP 状态 ↔ structure-contract 第 7 章的一致性由 tests/contract 守卫校验。
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    # 通用
    VALIDATION_ERROR = "VALIDATION_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # 设备
    DEVICE_ID_REQUIRED = "DEVICE_ID_REQUIRED"
    DEVICE_ID_INVALID = "DEVICE_ID_INVALID"
    # PDF
    PDF_UPLOAD_INVALID = "PDF_UPLOAD_INVALID"
    PDF_PARSE_FAILED = "PDF_PARSE_FAILED"
    PDF_TOC_MISSING = "PDF_TOC_MISSING"
    PDF_NOT_FOUND = "PDF_NOT_FOUND"
    # API Key
    API_KEY_UNAVAILABLE = "API_KEY_UNAVAILABLE"
    API_KEY_NOT_SET = "API_KEY_NOT_SET"
    # 任务
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    TASK_STATE_CONFLICT = "TASK_STATE_CONFLICT"
    TASK_NOT_RESUMABLE = "TASK_NOT_RESUMABLE"
    TASK_IN_PROGRESS = "TASK_IN_PROGRESS"
    GENERATION_FAILED = "GENERATION_FAILED"
    # 牌组/卡片
    DECK_NOT_FOUND = "DECK_NOT_FOUND"
    CARD_NOT_FOUND = "CARD_NOT_FOUND"
    GENERATION_ITEM_CONFLICT = "GENERATION_ITEM_CONFLICT"
    IMPORT_PARSE_ERROR = "IMPORT_PARSE_ERROR"
    # 复习
    REVIEW_EVENT_INVALID = "REVIEW_EVENT_INVALID"
    REVIEW_EVENT_CONFLICT = "REVIEW_EVENT_CONFLICT"


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.DEVICE_ID_REQUIRED: 401,
    ErrorCode.DEVICE_ID_INVALID: 401,
    ErrorCode.PDF_UPLOAD_INVALID: 400,
    ErrorCode.PDF_PARSE_FAILED: 422,
    ErrorCode.PDF_TOC_MISSING: 422,
    ErrorCode.PDF_NOT_FOUND: 404,
    ErrorCode.API_KEY_UNAVAILABLE: 502,
    ErrorCode.API_KEY_NOT_SET: 422,
    ErrorCode.TASK_NOT_FOUND: 404,
    ErrorCode.TASK_STATE_CONFLICT: 409,
    ErrorCode.TASK_NOT_RESUMABLE: 409,
    ErrorCode.TASK_IN_PROGRESS: 409,
    ErrorCode.GENERATION_FAILED: 500,
    ErrorCode.DECK_NOT_FOUND: 404,
    ErrorCode.CARD_NOT_FOUND: 404,
    ErrorCode.GENERATION_ITEM_CONFLICT: 409,
    ErrorCode.IMPORT_PARSE_ERROR: 422,
    ErrorCode.REVIEW_EVENT_INVALID: 400,
    ErrorCode.REVIEW_EVENT_CONFLICT: 409,
}

# 文案清单（唯一位置，R-01）：派生集合的显式快照，守卫校验与派生集合全等
LOCALIZATION_KEYS: frozenset[str] = frozenset(
    {
        "error.validation_error",
        "error.rate_limited",
        "error.idempotency_conflict",
        "error.internal_error",
        "error.device_id_required",
        "error.device_id_invalid",
        "error.pdf_upload_invalid",
        "error.pdf_parse_failed",
        "error.pdf_toc_missing",
        "error.pdf_not_found",
        "error.api_key_unavailable",
        "error.api_key_not_set",
        "error.task_not_found",
        "error.task_state_conflict",
        "error.task_not_resumable",
        "error.task_in_progress",
        "error.generation_failed",
        "error.deck_not_found",
        "error.card_not_found",
        "error.generation_item_conflict",
        "error.import_parse_error",
        "error.review_event_invalid",
        "error.review_event_conflict",
    }
)


def http_status(code: ErrorCode) -> int:
    return ERROR_HTTP_STATUS[code]


def localization_key(code: ErrorCode) -> str:
    """错误码 → localization_key 派生规则（R-01）。"""
    return "error." + code.value.lower()


class AppError(Exception):
    """统一业务错误：handler 层映射为 1.4 错误响应；message 仅面向用户，内部细节只进日志。"""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_response(self) -> dict[str, dict[str, str]]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "localization_key": localization_key(self.code),
            }
        }
```

- [ ] **Step 4: 运行确认通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_errors.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/errors.py tests/unit/test_errors.py`
Expected: PASS 且 ruff/mypy 无报错

- [ ] **Step 5: 提交**

```bash
git add main/app/errors.py main/tests/unit/test_errors.py
git commit -m "feat(errors): 统一错误对象/错误码注册表/localization_key 清单（R-01 解决）"
```

---

### Task 5: 错误响应 schema 与 schema↔openapi 守卫框架

**Files:**
- Modify: `main/app/schemas/common.py`（占位 docstring → 真实模型）
- Create: `main/tests/contract/support.py`（守卫辅助：文档解析与 schema 一致性框架）
- Create: `main/tests/contract/test_schemas_openapi_guard.py`

**Interfaces:**
- Consumes: Task 4 无（schema 守卫独立）；openapi.yaml 的 `Error` schema
- Produces: `app.schemas.common.ErrorDetail`/`ErrorResponse`（pydantic 模型，与 openapi `Error` 一致）；`tests.contract.support`：`REPO_ROOT`、`OPENAPI_PATH`、`STRUCTURE_CONTRACT_PATH`、`MANIFEST_PATH`、`load_openapi() -> dict[str, Any]`、`openapi_schema(name) -> dict[str, Any]`、`resolve_ref(schema, openapi) -> dict`、`check_schema_consistency(model, schema, openapi, path="$") -> list[str]`、`parse_error_codes_table(md_text) -> dict[str, int]`、`load_manifest() -> dict[str, Any]`、`extract_version_declarations(md_text) -> dict[str, str]`（后三者 Task 6 复用）

- [ ] **Step 1: 重写 `main/app/schemas/common.py`**

```python
"""通用 schema：统一错误响应（structure-contract 1.4 / openapi `Error`）。"""

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    localization_key: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
```

- [ ] **Step 2: 写失败守卫测试 `main/tests/contract/test_schemas_openapi_guard.py`**

```python
"""契约守卫 1：app/schemas ↔ openapi.yaml（project-structure 5，红线 1）。"""

from pydantic import BaseModel

from app.schemas.common import ErrorResponse
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_schema_openapi_error_consistent() -> None:
    violations = check_schema_consistency(ErrorResponse, openapi_schema("Error"), load_openapi())
    assert violations == []


def test_schema_guard_detects_extra_model_field() -> None:
    """负例：守卫必须具备真牙口——模型多出字段必须被检出。"""

    class Drifted(BaseModel):
        code: str
        message: str
        localization_key: str
        extra_field: str

    nested = openapi_schema("Error")["properties"]["error"]
    violations = check_schema_consistency(Drifted, nested, load_openapi())
    assert any("extra_field" in v for v in violations)


def test_schema_guard_detects_missing_required_field() -> None:
    """负例：openapi 必填字段在模型中可选必须被检出。"""

    class MissingRequired(BaseModel):
        code: str
        message: str
        localization_key: str | None = None

    nested = openapi_schema("Error")["properties"]["error"]
    violations = check_schema_consistency(MissingRequired, nested, load_openapi())
    assert any("localization_key" in v for v in violations)
```

- [ ] **Step 3: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/test_schemas_openapi_guard.py -v`
Expected: FAIL（ModuleNotFoundError: tests.contract.support）

- [ ] **Step 4: 实现 `main/tests/contract/support.py`**

```python
"""契约守卫辅助：解析 openapi.yaml / structure-contract.md / manifest.json 与 pydantic 模型比对。

权威来源（防漂移规则）：docs/Architecture/* 与 agent_evolution/manifest.json；
本模块只做解析比对，不建立第二套错误码/字段权威。F0 起全包复用，纵向包按需扩展。
"""

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic.fields import FieldInfo

REPO_ROOT = Path(__file__).resolve().parents[3]

OPENAPI_PATH = REPO_ROOT / "docs" / "Architecture" / "openapi.yaml"
STRUCTURE_CONTRACT_PATH = REPO_ROOT / "docs" / "Architecture" / "structure-contract.md"
MANIFEST_PATH = REPO_ROOT / "agent_evolution" / "manifest.json"

# openapi type → 期望 Python 注解（字符串枚举单独处理）
_TYPE_MAP: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}


def load_openapi() -> dict[str, Any]:
    with OPENAPI_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def openapi_schema(name: str) -> dict[str, Any]:
    return load_openapi()["components"]["schemas"][name]


def resolve_ref(schema: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    """解析 `$ref`（仅组件内引用），返回实际 schema。"""
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert isinstance(ref, str) and ref.startswith("#/components/schemas/")
    name = ref.removeprefix("#/components/schemas/")
    return openapi["components"]["schemas"][name]


def check_schema_consistency(
    model: type[BaseModel], schema: dict[str, Any], openapi: dict[str, Any], path: str = "$"
) -> list[str]:
    """递归比较 pydantic 模型与 openapi schema，返回违约列表（空 = 一致）。

    F0 覆盖：object 属性名与必填、string（含 enum）、integer/number/boolean/array 类型映射、
    $ref 解析；anyOf/oneOf/allOf 契约暂无，纵向包按需扩展。
    """
    violations: list[str] = []
    schema = resolve_ref(schema, openapi)
    model_fields: dict[str, FieldInfo] = model.model_fields
    props: dict[str, Any] = schema.get("properties", {})
    for name in props:
        if name not in model_fields:
            violations.append(f"{path}: openapi 属性 {name!r} 在模型中缺失")
    for name in model_fields:
        if name not in props:
            violations.append(f"{path}: 模型字段 {name!r} 在 openapi 中缺失")
    required = set(schema.get("required", []))
    for name in required:
        field = model_fields.get(name)
        if field is not None and not field.is_required():
            violations.append(f"{path}.{name}: openapi 必填但模型可选")
    for name, prop in props.items():
        if name not in model_fields:
            continue
        annotation: Any = model_fields[name].annotation
        resolved = resolve_ref(prop, openapi)
        prop_type = resolved.get("type")
        if prop_type == "object":
            violations.extend(
                check_schema_consistency(annotation, resolved, openapi, f"{path}.{name}")
            )
            continue
        expected = _TYPE_MAP.get(prop_type)
        if expected is None:
            violations.append(f"{path}.{name}: 未支持的 openapi type {prop_type!r}")
            continue
        if not _annotation_matches(annotation, expected):
            violations.append(f"{path}.{name}: openapi {prop_type!r} 与注解 {annotation!r} 不匹配")
        if prop_type == "string" and "enum" in resolved and _is_enum(annotation):
            member_values = {member.value for member in annotation}
            if member_values != set(resolved["enum"]):
                violations.append(f"{path}.{name}: openapi enum 与模型枚举不一致")
    return violations


def _is_enum(annotation: Any) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, Enum)


def _annotation_matches(annotation: Any, expected: Any) -> bool:
    if expected is str:
        return annotation is str or _is_enum(annotation)
    if expected is list:
        return getattr(annotation, "__origin__", None) is list
    return annotation is expected


def parse_error_codes_table(md_text: str) -> dict[str, int]:
    """解析 structure-contract 第 7 章错误码表 → {CODE: http_status}。"""
    section = md_text.split("## 7. 错误码表", 1)[1].split("## 8.", 1)[0]
    result: dict[str, int] = {}
    for line in section.splitlines():
        match = re.match(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|\s*(\d{3})\s*\|", line)
        if match:
            result[match.group(1)] = int(match.group(2))
    return result


def load_manifest() -> dict[str, Any]:
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def extract_version_declarations(md_text: str) -> dict[str, str]:
    """structure-contract 中显式声明的 prompt_version/schema_version/rubric_version（当前无，
    仅 8.5 引用 manifest 对应 version）。发现声明时守卫要求与 manifest 一致（Architecture AGENTS.md 6）。"""
    result: dict[str, str] = {}
    for key in ("prompt_version", "schema_version", "rubric_version"):
        for match in re.finditer(rf"\b{key}\b[^\w]*?(v\d+)", md_text):
            result[key] = match.group(1)
            break
    return result
```

- [ ] **Step 5: 运行确认通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/test_schemas_openapi_guard.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/schemas/ tests/contract/`
Expected: PASS 且 ruff/mypy 无报错

- [ ] **Step 6: 提交**

```bash
git add main/app/schemas/common.py main/tests/contract/support.py main/tests/contract/test_schemas_openapi_guard.py
git commit -m "feat(guard): 错误响应 schema + schema↔openapi 守卫框架（含负例）"
```

---

### Task 6: 错误码 / localization_key / manifest 契约守卫

**Files:**
- Create: `main/tests/contract/test_error_codes_guard.py`
- Create: `main/tests/contract/test_localization_guard.py`
- Create: `main/tests/contract/test_manifest_guard.py`

**Interfaces:**
- Consumes: Task 4 `app.errors`（`ErrorCode`/`ERROR_HTTP_STATUS`/`LOCALIZATION_KEYS`/`localization_key`）；Task 5 `tests.contract.support`（`STRUCTURE_CONTRACT_PATH`、`MANIFEST_PATH`、`parse_error_codes_table`、`load_manifest`、`extract_version_declarations`）
- Produces: 三道可运行守卫（守卫 3/4 + manifest 版本守卫），R-01 的自动化落点

- [ ] **Step 1: 写 `main/tests/contract/test_error_codes_guard.py`**

```python
"""契约守卫 3：错误码清单 ↔ structure-contract 第 7 章（project-structure 5，红线 1）。"""

from app.errors import ERROR_HTTP_STATUS, ErrorCode
from tests.contract.support import STRUCTURE_CONTRACT_PATH, parse_error_codes_table


def test_error_codes_match_contract_chapter7() -> None:
    doc_codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    code_registry = {code.value: ERROR_HTTP_STATUS[code] for code in ErrorCode}
    assert code_registry == doc_codes
```

- [ ] **Step 2: 写 `main/tests/contract/test_localization_guard.py`**

```python
"""契约守卫 4：localization_key ↔ 文案清单（project-structure 5；R-01 派生规则与唯一位置）。"""

import re

from app.errors import ErrorCode, LOCALIZATION_KEYS, localization_key


def test_localization_keys_match_derived_set() -> None:
    derived = frozenset(localization_key(code) for code in ErrorCode)
    assert derived == LOCALIZATION_KEYS


def test_localization_keys_format() -> None:
    for key in LOCALIZATION_KEYS:
        assert re.fullmatch(r"error\.[a-z0-9_]+", key) is not None
```

- [ ] **Step 3: 写 `main/tests/contract/test_manifest_guard.py`**

```python
"""契约守卫：agent_evolution/manifest.json ↔ structure-contract 版本引用（Architecture AGENTS.md 6）。"""

import json
import re

from tests.contract.support import (
    MANIFEST_PATH,
    STRUCTURE_CONTRACT_PATH,
    extract_version_declarations,
    load_manifest,
)


def test_manifest_asset_versions_and_paths_valid() -> None:
    manifest = load_manifest()
    assets = [
        ("prompts", "planner"),
        ("prompts", "generator"),
        ("schemas", "card"),
        ("rubrics", "main"),
    ]
    for section, name in assets:
        entry = manifest[section][name]
        assert re.fullmatch(r"v\d+", entry["version"]), f"{section}.{name} 版本格式非法"
        asset_path = MANIFEST_PATH.parent / entry["path"]
        assert asset_path.is_file(), f"{section}.{name} 资产文件缺失: {asset_path}"


def test_manifest_versions_match_structure_contract_declarations() -> None:
    manifest = load_manifest()
    prompt_versions = {entry["version"] for entry in manifest["prompts"].values()}
    expected = {
        "prompt_version": prompt_versions,
        "schema_version": {manifest["schemas"]["card"]["version"]},
        "rubric_version": {manifest["rubrics"]["main"]["version"]},
    }
    declared = extract_version_declarations(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    for key, versions in expected.items():
        if key in declared:
            assert declared[key] in versions, f"{key}: 契约声明 {declared[key]} 与 manifest 不一致"


def test_manifest_json_parseable() -> None:
    with MANIFEST_PATH.open(encoding="utf-8") as f:
        data = json.load(f)
    assert "prompts" in data and "schemas" in data and "rubrics" in data
```

- [ ] **Step 4: 运行确认通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/ -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy tests/contract/`
Expected: PASS 且 ruff/mypy 无报错

- [ ] **Step 5: 提交**

```bash
git add main/tests/contract/test_error_codes_guard.py main/tests/contract/test_localization_guard.py main/tests/contract/test_manifest_guard.py
git commit -m "feat(guard): 错误码/localization_key/manifest 契约守卫"
```

---

### Task 7: 应用装配、探针与统一错误 handler + 共享测试基座

**Files:**
- Modify: `main/app/main.py`（占位 docstring → create_app 装配）
- Modify: `main/app/middleware/error_handler.py`（占位 docstring → 注册 AppError handler）
- Create: `main/app/api/probes.py`
- Create: `main/infra/storage/local.py`
- Create: `main/tests/conftest.py`
- Test: `main/tests/integration/test_probes.py`、`main/tests/integration/test_error_handler.py`

**Interfaces:**
- Consumes: Task 2 `Settings`；Task 3 `create_db_engine`；Task 4 `AppError`/`ErrorCode`/`http_status`；`infra.storage.local.LocalStorage`（本任务创建）
- Produces: `app.main.create_app(settings: Settings | None = None) -> FastAPI`（模块级 `app` 供 uvicorn；`app.state.settings/engine/storage`；lifespan 关闭时 dispose engine）；`app.middleware.error_handler.register_exception_handlers(app: FastAPI) -> None`；`app.api.probes.router`（`GET /healthz`→200、`GET /readyz`→200/503，豁免鉴权，契约 8.2）；`infra.storage.local.LocalStorage`（`__init__(storage_path: Path)`、`check_writable() -> bool`）；`tests.conftest` fixtures：`settings`/`app`/`client`/`clock`

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_probes.py`**

```python
"""探针集成测试（structure-contract 8.2）：healthz 存活、readyz DB+存储真实检查。"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_probes_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_probes_readyz_ok_creates_empty_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ready.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert resp.json()["checks"] == {"database": "ok", "storage": "ok"}
    assert db_path.exists()  # 空测试库创建


def test_probes_readyz_db_unavailable_returns_503(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:////nonexistent-dir/app.db", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "error"


def test_probes_readyz_storage_unavailable_returns_503(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-dir", encoding="utf-8")
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'ok.db'}", storage_path=blocker / "sub")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["storage"] == "error"


def test_probes_healthz_alive_even_when_db_down(tmp_path: Path) -> None:
    settings = Settings(
        database_url="sqlite:////nonexistent-dir/app.db", storage_path=tmp_path / "storage"
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
```

- [ ] **Step 2: 写失败集成测试 `main/tests/integration/test_error_handler.py`**

```python
"""统一错误 handler 集成测试（structure-contract 1.4 / 红线 3）。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.errors import AppError, ErrorCode
from app.middleware.error_handler import register_exception_handlers


def test_error_handler_returns_contract_shape() -> None:
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/boom")
    def boom() -> None:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "未找到牌组")

    with TestClient(probe) as client:
        resp = client.get("/boom")
    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "DECK_NOT_FOUND",
            "message": "未找到牌组",
            "localization_key": "error.deck_not_found",
        }
    }
```

- [ ] **Step 3: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_probes.py tests/integration/test_error_handler.py -v`
Expected: FAIL（ModuleNotFoundError: app.main / app.middleware.error_handler / infra.storage.local / conftest）

- [ ] **Step 4: 实现 `main/infra/storage/local.py`**

```python
"""本地文件存储（infra/storage）：F0 提供就绪探针可写性检查；PDF 受控存储在 V3A 扩展。"""

import os
import uuid
from pathlib import Path


class LocalStorage:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path

    def check_writable(self) -> bool:
        """就绪探针（structure-contract 8.2）：目录可创建且可写。失败返回 False，不抛异常。"""
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            probe = self.storage_path / f".write-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False
```

- [ ] **Step 5: 实现 `main/app/middleware/error_handler.py`**

```python
"""统一错误响应 handler（structure-contract 1.4；红线 3：错误码格式统一于 app/middleware）。

F0：AppError → 1.4 错误响应；请求日志/request_id/通用异常包装随 F1 统一中间件接入。
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import AppError, http_status


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=http_status(exc.code), content=exc.to_response())
```

- [ ] **Step 6: 实现 `main/app/api/probes.py`**

```python
"""运行观测探针（structure-contract 8.2）：/healthz 存活、/readyz 就绪（DB + 存储），豁免 X-Device-ID 鉴权。"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["observability"])
logger = logging.getLogger(__name__)


@router.get("/healthz", status_code=200)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    db_ok = True
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        logger.warning("readyz database check failed: %s", exc)
    checks["database"] = "ok" if db_ok else "error"
    storage_ok = request.app.state.storage.check_writable()
    checks["storage"] = "ok" if storage_ok else "error"
    if not (db_ok and storage_ok):
        return JSONResponse(status_code=503, content={"status": "unavailable", "checks": checks})
    return JSONResponse(content={"status": "ready", "checks": checks})
```

- [ ] **Step 7: 重写 `main/app/main.py`**

```python
"""应用装配（project-structure 3）：唯一创建入口 create_app；模块级 app 供 uvicorn 启动。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import probes
from app.config import Settings
from app.middleware.error_handler import register_exception_handlers
from infra.db.session import create_db_engine
from infra.storage.local import LocalStorage


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_db_engine(settings.database_url)
    storage = LocalStorage(settings.storage_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(probes.router)
    app.state.settings = settings
    app.state.engine = engine
    app.state.storage = storage
    return app


app = create_app()
```

- [ ] **Step 8: 实现 `main/tests/conftest.py` 共享测试基座**

```python
"""共享测试基座（Progress F0）：隔离 Settings、临时 DB/存储、TestClient、可控时钟。

隔离测试配置：fixture 一律显式构造 Settings（临时目录），不加载任何 .env/环境配置；
时钟经 infra.clock 注入，服务代码只能通过 Clock 接口取时间。
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from infra.clock import FrozenClock


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}", storage_path=tmp_path / "storage")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC))
```

- [ ] **Step 9: 运行确认全部通过 + 格式/静态检查**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全部测试 PASS，ruff/mypy 零报错

- [ ] **Step 10: 提交**

```bash
git add main/app/main.py main/app/middleware/error_handler.py main/app/api/probes.py main/infra/storage/local.py main/tests/conftest.py main/tests/integration/test_probes.py main/tests/integration/test_error_handler.py
git commit -m "feat(app): create_app 装配 + healthz/readyz 探针 + 统一错误 handler + 测试基座"
```

---

### Task 8: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 F0 产物；不新增代码

**Interfaces:**
- Consumes: Task 1~7 全部产物

- [ ] **Step 1: 四个工具命令全绿**

Run（均在 `main/`）:
```bash
conda run -n shanka-backend python --version                      # 期望 Python 3.12.13
conda run -n shanka-backend python -m pytest
conda run -n shanka-backend python -m ruff check .
conda run -n shanka-backend python -m ruff format --check .
conda run -n shanka-backend python -m mypy .
```
Expected: 版本 3.12.x；四命令零失败

- [ ] **Step 2: 干净环境按锁定结果安装成功**

```bash
conda run -n shanka-backend python -m venv /tmp/f0-accept-venv
/tmp/f0-accept-venv/bin/pip install -r /home/kbzz1/shanka_backend/main/requirements-dev.lock
/tmp/f0-accept-venv/bin/pip install -e /home/kbzz1/shanka_backend/main
cd /home/kbzz1/shanka_backend/main && /tmp/f0-accept-venv/bin/python -c "import app.main, app.errors, sqlalchemy; print('clean-install-ok')"
rm -rf /tmp/f0-accept-venv
```

- [ ] **Step 3: 应用启动冒烟（真实 uvicorn）**

```bash
cd /home/kbzz1/shanka_backend/main
conda run -n shanka-backend uvicorn app.main:app --port 8099 > /tmp/f0-uvicorn.log 2>&1 &
UV_PID=$!
sleep 3
curl -s -o /dev/null -w "healthz=%{http_code}\n" http://127.0.0.1:8099/healthz   # 期望 200
curl -s -o /dev/null -w "readyz=%{http_code}\n" http://127.0.0.1:8099/readyz     # 期望 200
kill $UV_PID
```
Expected: healthz=200、readyz=200；`main/shanka.db` 已创建（git 忽略）

- [ ] **Step 4: 契约守卫与 readyz 成败边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/contract tests/integration/test_probes.py -v`
Expected: 全绿；记录测试输出中守卫用例与 readyz 200/503 用例名称作为验收证据

- [ ] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app --include="*.py" || true`
Expected: 无输出（实现代码中不得出现 API Key 明文形态；tests 中仅允许测试用假值 `sk-super-secret-value` 存在）

---

### Task 9: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`

**Interfaces:**
- Consumes: Task 8 验收证据

- [ ] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 F0 行：`TODO` → `DONE`，当前证据填写：可编辑安装+锁定（requirements-dev.lock 干净环境复现）、四工具命令全绿、healthz/readyz 200/503 集成测试、空测试库创建、四类守卫（schema↔openapi/错误码/localization/manifest）用例通过、uvicorn 启动冒烟。
- 第 6 节：R-01 → `RESOLVED`（派生规则+唯一位置：`app/errors.py`，清单 `LOCALIZATION_KEYS`，守卫全等校验）；R-02 → `RESOLVED`（hatchling + pip-tools 锁定，`requirements-dev.lock`）；R-07 → `RESOLVED`（Superpowers 插件已安装，本会话可用 writing-plans/subagent-driven-development）。
- 第 1 节状态基线：`可运行后端` → `DONE`（F0 基线：装配+探针；业务路由随 V1+ 纵向包）、`自动化验证` → `DONE`（F0：unit/integration/contract 首批用例）。
- 计划文件标题下「结果」注明 F0 DONE 与证据位置。

- [ ] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-10-f0-executable-baseline-and-guards.md
git commit -m "docs(progress): F0 DONE（可执行基线+防漂移护栏），R-01/R-02/R-07 RESOLVED"
```

---

## Self-Review

**1. Spec coverage（对照 Progress F0 文本）：**

| F0 要求 | 落点 |
| --- | --- |
| 补齐 build backend/package discovery，`pip install -e .[dev]` 可用 | Task 1（hatchling + 包列表 + 实测安装） |
| 生成锁定文件 | Task 1 Step 4/5（pip-tools + 干净环境复现） |
| 建立单一 Settings | Task 2 |
| 应用装配 | Task 7（create_app） |
| DB session | Task 3（engine + WAL/外键 + 空测试库） |
| 隔离测试配置 | Task 7 conftest（显式 Settings + 临时目录） |
| 统一错误对象/错误码/localization key 清单 | Task 4（R-01 派生规则 + 显式清单） |
| contract 守卫（schema/OpenAPI、错误码、localization key、manifest） | Task 5/6 四道守卫 |
| 测试 client、临时 DB/存储、可控时钟 | Task 7 conftest（client/settings/clock fixture） |
| healthz；readyz 在 DB/存储不可用时真实 503 | Task 7 probes + 集成测试（两个失败分支） |
| 验收：python 3.12、干净安装、四命令、启动、空库、守卫、readyz 成败 | Task 8 |
| 开工前解决 R-01 | Task 4 + Task 6 守卫 + Task 9 登记 |
| 解决 R-02 | Task 1 + Task 9 登记 |
| R-07（Superpowers 可用） | Task 9 登记 RESOLVED |

**2. Placeholder scan：** 全部任务均给出完整文件内容与可执行命令；无 TBD/TODO 占位；F1 归属事项（BEGIN IMMEDIATE、中间件、metrics、ORM）以明确边界声明，不写"以后再说"式空步骤。

**3. Type consistency：** `Settings(database_url=, storage_path=)` 构造在 Task 2 定义、Task 7 conftest/探针测试使用；`create_db_engine(database_url: str)` 在 Task 3 定义、Task 7 装配使用；`LocalStorage(storage_path: Path).check_writable() -> bool` 在 Task 7 定义与使用；`AppError(code, message).to_response()`、`http_status(code)`、`localization_key(code)` 在 Task 4 定义、Task 5/6/7 使用；`check_schema_consistency(model, schema, openapi, path="$")` 在 Task 5 定义、测试使用；`parse_error_codes_table`/`load_manifest`/`extract_version_declarations`/`*_PATH` 常量在 Task 5 定义、Task 6 使用；fixtures `settings`/`app`/`client`/`clock` 在 Task 7 定义、探针测试使用。Task 3 中 `_configure_connection` 的 `connection_record` 参数未被使用（connect 事件签名要求），无引用遗漏。
