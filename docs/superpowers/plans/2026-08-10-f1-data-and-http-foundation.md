# F1 数据与 HTTP 共享基础 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**结果**：<主 Agent 整包验收通过后在此注明 F1 DONE 与证据位置>

**Goal:** 实现 12 张表的 ORM 与 Alembic 初始迁移、统一设备鉴权（X-Device-ID + 自动注册 + 探针豁免）、request_id + JSON 结构化日志、统一错误包装（VALIDATION_ERROR/INTERNAL_ERROR）、Idempotency-Key 幂等原语（并发占位/同事务/首次响应重放）、内存限流（5 维度 429 + Retry-After）、Prometheus /metrics 端点与 HTTP/限流指标，使 F1 依据真实验收证据标记 DONE。

**Architecture:** 契约驱动分层，`app → services → infra` 单向。F1 建立在 F0 地基上（`app/config.py`、`infra/db/session.py`、`infra/clock.py`、`app/errors.py`、`app/main.py`、`app/middleware/error_handler.py`、`app/api/probes.py`、`tests/contract/support.py`、`tests/conftest.py`）。新增：`infra/db/models.py`（12 表 ORM，database-design 2.1~2.12 一一对应）、`main/alembic.ini` + `main/migrations/`（初始迁移）、`app/middleware/request_id.py` + `app/middleware/logging.py`（O-1）、`infra/logging.py`（JSONFormatter）、`app/middleware/device_id.py`（1.1 + database-design 2.1）、`app/middleware/idempotency.py`（1.3 原语，handler 在请求级 session 内调用，保证幂等 INSERT 与业务副作用同事务）、`app/middleware/rate_limit.py`（1.6 内存限流）、`app/api/metrics.py`（8.3，R-04 不进 OpenAPI）。中间件按红线 3 集中于 `app/middleware/`；请求级 session 由 `infra/db/session.py` 提供 FastAPI dependency；事务语义归 service（F1 只提供原语与测试，完整同事务验收在 V1 首个写接口）。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0（engine + sessionmaker + ORM）、Alembic、prometheus-client、pytest、ruff、mypy strict。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行（在 `main/` 下），禁止向 base/系统 Python 装依赖。
- 契约权威：structure-contract 1.1~1.7（鉴权/时间/幂等/错误/Key 安全/限流）、8.1（JSON 日志）、8.2（探针）、8.3（指标）、database-design 0~3（表/约束/级联/并发）。实现不得修改 `docs/PRD/`、`docs/Architecture/structure-contract.md`、`docs/Architecture/openapi.yaml`。
- **契约兼容性更新**（唯一例外，Task 8）：`database-design.md` §2.12 idempotency_keys 新增列 `request_body_hash`（契约 1.3 要求"幂等键相同但请求体与首次不一致 → 409"，但表设计无比对载体）。加列属兼容性变更（AGENTS.md 版本管理），只更新 database-design.md，不同步 PRD；同时登记 Progress 第 6 节新冲突 R-10（Task 12）。
- 时间格式唯一规范（database-design §0）：`YYYY-MM-DDTHH:MM:SS.sssZ`，统一 `format_utc`；naive datetime 抛 ValueError。
- 连接配置（database-design §0/3）：`PRAGMA journal_mode=WAL` + `PRAGMA foreign_keys=ON` engine 级 connect 事件（F0 已有）；写事务 `BEGIN IMMEDIATE`（engine `begin` 事件），覆盖请求级 session 与迁移。
- 枚举存 TEXT（database-design §0 类型映射）；UUID → TEXT；时间 → TEXT；JSON → TEXT；布尔 → INTEGER(0/1)；小数 → REAL。
- API Key 明文禁止打印/复制/写入日志、fixture、测试报告；通用请求日志**不记录任何请求体**（天然满足 1.5 对 `PUT /api-key` 请求体强制掩码的红线）。
- 探针/指标端点（`/healthz`、`/readyz`、`/metrics`）豁免 X-Device-ID 鉴权（8.2/8.3）；IP 限流（5 req/s）覆盖全部接口（1.6 表）。
- 跨设备资源访问统一 404 不暴露存在性（1.1）：F1 通过错误码（`DECK_NOT_FOUND` 等已 404）+ 设计规则落实，纵向包按 device_id 过滤实现。
- ruff line-length 100、mypy strict；四工具命令全绿才算完成：`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`（均在 `main/`）。
- 测试命名 `test_<模块>_<行为>`；unit 不触 DB/网络/文件；integration 走真实临时 SQLite/文件系统；contract 解析正式文档。
- 提交只 `git add` 本任务文件，禁止卷入工作区既有未提交改动。
- 工作包边界：F1 不含业务路由（V1+）、PDF 存储管理（V3A）、Key 加密与 DeepSeek（V3B）、llm/generation/batch 指标（V3B/V5A）、看板聚合（V2）。`app/api/` 下业务占位模块（decks.py 等）不得改动。
- 幂等/限流/设备中间件的实现在 `app/middleware/` 统一（红线 3），禁止散落各处。
- Task 1~10 由实现 subagent 完成；Task 11/12 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: 依赖新增与 DB session 扩展（sessionmaker + BEGIN IMMEDIATE + 请求级 session）

**Files:**
- Modify: `main/pyproject.toml`（dependencies 加 `alembic>=1.13`、`prometheus-client>=0.20`）
- Modify: `main/infra/db/session.py`（扩展）
- Create: `main/tests/integration/test_session_transaction.py`
- Modify: `main/requirements-dev.lock`（pip-compile 再生成）

**Interfaces:**
- Consumes: F0 `create_db_engine`（现有 WAL/外键 connect 事件）
- Produces: `create_session_factory(engine) -> sessionmaker[Session]`；engine `begin` 事件 → `BEGIN IMMEDIATE`（写事务，database-design §0/3）；`get_db_session()` FastAPI dependency（yield session，服务退出不回滚——事务语义归 service，dependency 只提供 session 与 close）；`infra.db.session.TIME_FORMAT_RE` 不需导出

- [ ] **Step 1: 更新 pyproject 依赖**

在 `main/pyproject.toml` 的 `dependencies` 中追加：

```toml
  "alembic>=1.13",
  "prometheus-client>=0.20",
```

- [ ] **Step 2: 安装并再生成锁定文件**

Run:
```bash
cd /home/kbzz1/shanka_backend/main
conda run -n shanka-backend pip install -e ".[dev]"
conda run -n shanka-backend pip-compile pyproject.toml --extra dev --output-file requirements-dev.lock
```
Expected: 安装成功；lock 含 alembic 与 prometheus-client 钉版本

- [ ] **Step 3: 写失败集成测试 `main/tests/integration/test_session_transaction.py`**

```python
"""DB session 事务语义集成测试（database-design 0/3）：BEGIN IMMEDIATE + 回滚 + 请求级 session。"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.main import create_app
from infra.db.session import create_db_engine, create_session_factory


def test_session_begin_immediate_rollback_releases_lock(tmp_path: Path) -> None:
    """写事务 BEGIN IMMEDIATE：同库第二个写事务在第一个 commit/rollback 前必须等待（串行单写者）。"""
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tx.db'}")
    factory = create_session_factory(engine)
    # 建一张测试表（本任务无 ORM 表）
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"))
    with factory() as session:
        session.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        session.rollback()  # 未 commit → 释放
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM t")).scalar() == 0


def test_session_commit_persists(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'tx2.db'}")
    factory = create_session_factory(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL)"))
    with factory() as session:
        session.execute(text("INSERT INTO t (id, v) VALUES (1, 'a')"))
        session.commit()
    with factory() as session:
        assert session.execute(text("SELECT count(*) FROM t")).scalar() == 1


def test_session_get_db_dependency_yields_session() -> None:
    """请求级 session dependency：TestClient 请求内可执行 SQL。"""

    def create_probe_app() -> FastAPI:
        engine = create_db_engine("sqlite:///:memory:")
        factory = create_session_factory(engine)
        app = FastAPI()
        app.state.engine = engine
        app.state.session_factory = factory

        @app.get("/ping-db")
        def ping_db(session: Session = get_db_session()) -> dict[str, int]:
            assert session.execute(text("SELECT 1")).scalar() == 1
            return {"ok": 1}

        return app

    with TestClient(create_probe_app()) as client:
        resp = client.get("/ping-db")
    assert resp.status_code == 200
    assert resp.json() == {"ok": 1}
```

- [ ] **Step 4: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_session_transaction.py -v`
Expected: FAIL（ImportError: create_session_factory / get_db_session 未定义）

- [ ] **Step 5: 扩展 `main/infra/db/session.py`**

在现有文件基础上追加（保留现有 `create_db_engine`/`format_utc` 原样）：

```python
from collections.abc import Iterator

from sqlalchemy.orm import Session, sessionmaker

# database-design §0/3：写事务 BEGIN IMMEDIATE（进入即拿写锁，避免并发写 SQLITE_BUSY）。
# SQLite MVP 单写者：engine 级 begin 事件统一处理，覆盖请求级 session 与迁移脚本。
@event.listens_for(Engine, "begin")
def _begin_immediate(conn: Any) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
```

注意：`_begin_immediate` 注册到 `Engine` 类级事件会作用于**全部** engine（含测试内存库），符合 database-design §0"engine 级 connect/begin 事件统一配置"。若 mypy 对 `@event.listens_for(Engine, "begin")` 的 target 类型报错，改用 `@event.listens_for(engine, "begin")` 实例级注册——但实例级需在 `create_db_engine` 内注册（对本任务测试用 `create_db_engine` 创建的 engine 生效）：

```python
def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(...)  # 现有代码不变
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_connection)
        # database-design §0/3：写事务 BEGIN IMMEDIATE
        event.listen(engine, "begin", _begin_immediate)
    return engine
```

再在 `create_db_engine` 之后追加：

```python
from fastapi import Request

def get_db_session(request: Request) -> Iterator[Session]:
    """请求级 session（FastAPI dependency，F1 起注入 handler）。

    事务语义归 service：本 dependency 只创建 session、请求结束关闭；
    提交/回滚由调用方（service 用例）显式控制，禁止在 infra helper 内 commit。
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()
```

- [ ] **Step 6: 运行确认通过 + 格式/静态检查**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_session_transaction.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/ infra/ tests/`
Expected: PASS；注意 `create_app` 尚未把 `session_factory` 放进 state——`test_session_get_db_dependency_yields_session` 用自己的 probe app 设置 `app.state.session_factory`，不依赖 `create_app`；但 `create_app` 需要在本任务就补上 `app.state.session_factory = create_session_factory(engine)`（Task 6 的 device 中间件要消费它，先放进来无副作用）。若 mypy 报 `app.state` 类型，用 `cast` 或在 `create_app` 内赋值后 `assert hasattr` 模式（参考仓库现有写法）。

- [ ] **Step 7: 提交**

```bash
git add main/pyproject.toml main/requirements-dev.lock main/infra/db/session.py main/app/main.py main/tests/integration/test_session_transaction.py
git commit -m "feat(db): 依赖（alembic/prometheus-client）+ BEGIN IMMEDIATE 写事务 + 请求级 session"
```

---

### Task 2: ORM 模型 12 张表

**Files:**
- Create: `main/infra/db/models.py`

**Interfaces:**
- Consumes: SQLAlchemy 2.0（database-design 0 类型映射）
- Produces: `Base`（`DeclarativeBase`）；12 表模型：`Device`、`ApiKey`、`PdfFile`、`Chapter`、`Task`、`KnowledgePoint`、`Batch`、`Deck`、`Card`、`ReviewState`、`ReviewEvent`、`IdempotencyKey`；表名与 database-design §2 完全一致（`devices/api_keys/pdf_files/chapters/tasks/knowledge_points/batches/decks/cards/review_states/review_events/idempotency_keys`）；Task 3 迁移与 Task 4 守卫消费

- [ ] **Step 1: 实现 `main/infra/db/models.py`**

完整代码（database-design 2.1~2.12 逐表；注意：UUID/时间/JSON→TEXT、布尔→INTEGER、枚举→TEXT、小数→REAL；约束/索引/外键/FK 级联全部按表定义；**不要**用 SQLAlchemy Enum 类型，全部 String）：

```python
"""ORM 模型（database-design 2.1~2.12 一一对应，契约守卫 2 校验）。

类型映射（database-design §0）：UUID→TEXT、时间→TEXT(ISO 8601 UTC)、JSON→TEXT、
布尔→INTEGER(0/1)、小数→REAL、枚举→TEXT。
枚举值域由 domain/enums 与应用层校验保证，DB 层仅存字符串。
"""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Real,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Device(Base):
    """2.1 devices：匿名设备 ID 数据主体。"""

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    last_active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class ApiKey(Base):
    """2.2 api_keys：一设备一 Key，加密存储（V3B 使用）。"""

    __tablename__ = "api_keys"

    device_id: Mapped[str] = mapped_column(
        String, ForeignKey("devices.device_id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # AVAILABLE/INVALID/INSUFFICIENT_BALANCE/UNKNOWN
    masked_key: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class PdfFile(Base):
    """2.3 pdf_files：PDF 元数据，storage_key 为随机 UUID 存储路径。"""

    __tablename__ = "pdf_files"
    __table_args__ = (Index("ix_pdf_files_device_created", "device_id", "created_at"),)

    file_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/PARSING/PARSED/FAILED
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class Chapter(Base):
    """2.4 chapters：章节（用户可改 name/start_page/end_page）。"""

    __tablename__ = "chapters"
    __table_args__ = (Index("ix_chapters_file_id", "file_id"),)

    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    start_page: Mapped[int] = mapped_column(Integer, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, nullable=False)


class Task(Base):
    """2.5 tasks：生成任务（file_id/deck_id 删除后 SET NULL 保留任务）。"""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_device_created", "device_id", "created_at"),
        Index("ix_tasks_task_device", "task_id", "device_id"),
    )

    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    file_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("pdf_files.file_id", ondelete="SET NULL"), nullable=True
    )
    deck_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_chapters: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 快照
    generation_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    generated_card_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_batch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_batch_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resumable: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str | None] = mapped_column(String, nullable=True)


class KnowledgePoint(Base):
    """2.6 knowledge_points：知识点规划。"""

    __tablename__ = "knowledge_points"
    __table_args__ = (Index("ix_knowledge_points_task_id", "task_id"),)

    knowledge_point_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    chapter_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_chunk_id: Mapped[str] = mapped_column(String, nullable=False)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)  # PENDING/PROCESSED/SKIPPED


class Batch(Base):
    """2.7 batches：生成批次（游标完整性 UNIQUE(task_id, batch_index)）。"""

    __tablename__ = "batches"
    __table_args__ = (UniqueConstraint("task_id", "batch_index", name="uq_batches_task_index"),)

    batch_id: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    batch_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    generated_item_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    coverage_rate: Mapped[float | None] = mapped_column(Real, nullable=True)
    duplicate_rate: Mapped[float | None] = mapped_column(Real, nullable=True)
    difficulty_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    chapter_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_type_distribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty_deviation: Mapped[float | None] = mapped_column(Real, nullable=True)
    cache_hit_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_miss_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    rubric_version: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str | None] = mapped_column(String, nullable=True)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)


class Deck(Base):
    """2.8 decks：牌组。

    契约观察（登记 Progress R-11，V4 裁决）：structure-contract 3.8 Deck.source 为
    MANUAL/IMPORTED/GENERATED，而 database-design 2.8 只列 MANUAL/IMPORTED——
    字段权威在 structure-contract，database-design 派生遗漏 GENERATED 枚举说明。
    F1 建表用 TEXT 无 DB CHECK，不受影响；V4 创建 GENERATED 牌组时若需确认落点再更新 database-design。
    """

    __tablename__ = "decks"
    __table_args__ = (Index("ix_decks_device_updated", "device_id", "updated_at"),)

    deck_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # MANUAL/IMPORTED
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class Card(Base):
    """2.9 cards：卡片（部分唯一索引 generation_item_id；UNIQUE(deck_id, position)）。"""

    __tablename__ = "cards"
    __table_args__ = (
        UniqueConstraint("deck_id", "position", name="uq_cards_deck_position"),
        Index("ix_cards_device_deck", "device_id", "deck_id"),
        Index(
            "ix_cards_gen_item_partial",
            "generation_item_id",
            unique=True,
            sqlite_where=(
                "source = 'GENERATED' AND generation_item_id IS NOT NULL"
            ),
        ),
    )

    card_id: Mapped[str] = mapped_column(String, primary_key=True)
    deck_id: Mapped[str] = mapped_column(
        String, ForeignKey("decks.deck_id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # GENERATED/MANUAL/IMPORTED
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(String, nullable=True)
    card_type: Mapped[str] = mapped_column(String, nullable=False)  # QUESTION/TRUE_FALSE
    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_boolean: Mapped[int | None] = mapped_column(Integer, nullable=True)
    generation_item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    target_difficulty: Mapped[str | None] = mapped_column(String, nullable=True)
    knowledge_point_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON 数组
    evidence_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correctness_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    learning_value_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rubric_total_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ReviewState(Base):
    """2.10 review_states：FSRS 排程状态快照（与卡片一对一）。"""

    __tablename__ = "review_states"
    __table_args__ = (
        CheckConstraint("stability >= 0", name="ck_review_states_stability"),
        CheckConstraint(
            "difficulty >= 1 AND difficulty <= 10", name="ck_review_states_difficulty"
        ),
        Index("ix_review_states_due", "due"),
    )

    review_state_id: Mapped[str] = mapped_column(String, primary_key=True)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String, nullable=False)  # NEW/LEARNING/REVIEW/RELEARNING
    stability: Mapped[float] = mapped_column(Real, nullable=False)
    difficulty: Mapped[float] = mapped_column(Real, nullable=False)
    due: Mapped[str] = mapped_column(String, nullable=False)
    last_review: Mapped[str | None] = mapped_column(String, nullable=True)
    reps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_rating: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ReviewEvent(Base):
    """2.11 review_events：不可变复习事件（UNIQUE(device_id, client_event_id)）。"""

    __tablename__ = "review_events"
    __table_args__ = (
        UniqueConstraint("device_id", "client_event_id", name="uq_review_events_device_client"),
        Index("ix_review_events_device_reviewed", "device_id", "reviewed_at"),
        Index("ix_review_events_card_id", "card_id"),
    )

    review_event_id: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, ForeignKey("devices.device_id"), nullable=False)
    card_id: Mapped[str] = mapped_column(
        String, ForeignKey("cards.card_id", ondelete="CASCADE"), nullable=False
    )
    client_event_id: Mapped[str] = mapped_column(String, nullable=False)
    rating: Mapped[str] = mapped_column(String, nullable=False)  # AGAIN/HARD/GOOD/EASY
    reviewed_at: Mapped[str] = mapped_column(String, nullable=False)
    device_timezone: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class IdempotencyKey(Base):
    """2.12 idempotency_keys：幂等（复合主键 device_id+path+key；F1 Task 8 增加 request_body_hash 列）。"""

    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, primary_key=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[str] = mapped_column(Text, nullable=False)  # JSON 快照
    created_at: Mapped[str] = mapped_column(String, nullable=False)
```

（注意：`Index` 部分唯一索引的 `sqlite_where` 在 SQLAlchemy 2.0 用 `sqlite_where=` 参数——该参数在迁移 DDL 中产生 `WHERE` 子句，Task 3 迁移里用 `op.create_index(..., sqlite_where=text(...))` 对应。）

- [ ] **Step 2: 运行确认可导入**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -c "from infra.db.models import Base; print(sorted(Base.metadata.tables.keys()))"`
Expected: 12 个表名全部列出

- [ ] **Step 3: 验证 schema 创建与约束（临时集成检查）**

Run:
```bash
conda run -n shanka-backend python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import text
from infra.db.session import create_db_engine
from infra.db.models import Base

with TemporaryDirectory() as d:
    engine = create_db_engine(f"sqlite:///{Path(d) / 'm.db'}")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        print(sorted(tables))
PY
```
Expected: 12 表（含 alembic 无、sqlite_sequence 无）

- [ ] **Step 4: ruff/mypy**

Run: `conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy infra/db/models.py`
Expected: 全绿（`datetime` 导入若未使用会被 ruff F401 检出，请移除未使用导入）

- [ ] **Step 5: 提交**

```bash
git add main/infra/db/models.py
git commit -m "feat(db): 12 张表 ORM 模型（database-design 2.1~2.12）"
```

---

### Task 3: Alembic 初始迁移 + 迁移测试

**Files:**
- Create: `main/alembic.ini`、`main/migrations/env.py`、`main/migrations/script.py.mako`、`main/migrations/versions/0001_initial.py`
- Create: `main/tests/integration/test_alembic_migration.py`
- Modify: `main/.gitignore`（若需要——检查 `migrations/` 无产物）

**Interfaces:**
- Consumes: Task 2 `Base.metadata`
- Produces: 初始迁移 0001_initial（12 表 + 索引 + 部分唯一索引 + CHECK + FK 级联，与 ORM 一致）；`alembic upgrade head` / `downgrade base` 可空库往返；conftest 的迁移 fixture（Task 5 起 integration 测试使用）

- [ ] **Step 1: 写失败迁移测试 `main/tests/integration/test_alembic_migration.py`**

```python
"""Alembic 迁移集成测试：空库 upgrade → 12 表 + 约束；downgrade → 空库；再 upgrade 恢复。"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from infra.db.session import create_db_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "main" / "alembic.ini"


@pytest.fixture
def alembic_env(tmp_path: Path) -> tuple[object, Path]:
    """返回 (config, db_path)：config 指向真实 alembic.ini 且 sqlalchemy.url 指向临时库。"""
    db_path = tmp_path / "migrated.db"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config, db_path


def _table_names(db_path: Path) -> set[str]:
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        return {
            r[0]
            for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }


def test_alembic_upgrade_creates_all_tables(alembic_env: tuple[object, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    tables = _table_names(db_path)
    expected = {
        "devices", "api_keys", "pdf_files", "chapters", "tasks", "knowledge_points",
        "batches", "decks", "cards", "review_states", "review_events", "idempotency_keys",
        "alembic_version",
    }
    assert expected <= tables


def test_alembic_downgrade_empties_db(alembic_env: tuple[object, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    assert _table_names(db_path) == set()


def test_alembic_upgrade_downgrade_upgrade_roundtrip(alembic_env: tuple[object, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    tables = _table_names(db_path)
    assert "cards" in tables and "review_states" in tables


def test_alembic_foreign_keys_and_checks_active(alembic_env: tuple[object, Path]) -> None:
    """磁盘 SQLite：外键与 CHECK 约束真实生效（database-design 0/3）。"""
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        # 部分唯一索引存在
        indexes = {
            r[0]
            for r in conn.execute(text("PRAGMA index_list('cards')"))
        }
    assert "ix_cards_gen_item_partial" in indexes
    assert "uq_cards_deck_position" in indexes
```

- [ ] **Step 2: 初始化 Alembic 骨架**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend alembic init migrations`
Expected: 生成 `alembic.ini` 与 `migrations/` 骨架（env.py、script.py.mako、versions/ 空目录）
然后修改 `alembic.ini`（关键项）：

```ini
# alembic.ini 片段（保留 alembic init 其余内容）
[alembic]
script_location = migrations
# 运行时 URL 由 env.py 从环境变量 DATABASE_URL 或命令行 -x 提供；此占位仅保证离线测试可构造 Config
sqlalchemy.url = sqlite:///./shanka.db
```

- [ ] **Step 3: 重写 `main/migrations/env.py`**

```python
"""Alembic 迁移环境：URL 优先级 = 命令行 -x database_url=... > 环境变量 DATABASE_URL > alembic.ini 占位。

与 ORM 共用 models.Base.metadata（database-design §0：ORM 与迁移一致）；
SQLite 连接事件（WAL/外键/BEGIN IMMEDIATE）在迁移连接上同样生效——
env.py 通过 sqlalchemy.engine_from_config 创建 engine 时复用 infra.db.session 的
create_db_engine（其 connect/begin 事件覆盖迁移脚本）。
"""

from logging.config import fileConfig
from os import environ
from pathlib import Path
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

# 使 main/ 可导入（alembic 从 migrations/ 启动时 cwd=main/，正常路径可导入）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infra.db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# 命令行 -x database_url=... 优先
_x_args = dict(x.split("=", 1) for x in context.get_x_argument(as_dictionary=True).items())
if "database_url" in _x_args:
    config.set_main_option("sqlalchemy.url", _x_args["database_url"])
elif environ.get("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", environ["DATABASE_URL"])


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

（注：迁移连接上 WAL/外键由 `infra.db.session.create_db_engine` 的 connect 事件覆盖——此处 `engine_from_config` 未复用该函数。为满足 database-design §0"覆盖池化连接、后台任务与迁移脚本"，把 engine 创建改为复用 `create_db_engine`：）

```python
def run_migrations_online() -> None:
    from infra.db.session import create_db_engine

    url = config.get_main_option("sqlalchemy.url")
    connectable = create_db_engine(url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
```

- [ ] **Step 4: 生成初始迁移**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend alembic -x database_url="sqlite:////tmp/f1-autogen.db" revision --autogenerate -m "initial 12 tables"`
Expected: 生成 `migrations/versions/<hash>_initial_12_tables.py`；**审查自动生成内容**：
- 表名/列/类型与 database-design 一致
- `cards` 的部分唯一索引自动生成可能带 `sqlite_where=sa.text(...)`——核对 WHERE 子句为 `source = 'GENERATED' AND generation_item_id IS NOT NULL`
- 若有差异，手工修正迁移文件（迁移文件是权威 DDL，与 ORM 一致性由 Task 4 守卫 + 迁移测试兜底）

- [ ] **Step 5: 运行迁移测试确认通过**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_alembic_migration.py -v`
Expected: 4 passed；若 autogenerate 与 ORM 有偏差导致断言失败（如表缺失），修正迁移文件后重跑

- [ ] **Step 6: 清理 autogen 临时库 + ruff/mypy**

Run: `rm -f /tmp/f1-autogen.db && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy migrations/ infra/db/ tests/integration/`
Expected: 全绿（mypy 对 migrations/ 可加 `# type: ignore` 若 alembic 类型桩不足——尽量不忽略，先在 pyproject `[tool.mypy]` 无特殊配置前提下尝试）

- [ ] **Step 7: 提交**

```bash
git add main/alembic.ini main/migrations/ main/tests/integration/test_alembic_migration.py
git commit -m "feat(db): Alembic 初始迁移（12 表 + 约束/索引/级联）+ 往返迁移测试"
```

---

### Task 4: ORM ↔ database-design 契约守卫（守卫 2）

**Files:**
- Create: `main/tests/contract/test_orm_database_guard.py`
- Modify: `main/tests/contract/support.py`（追加 database-design 解析函数）

**Interfaces:**
- Consumes: Task 2 `infra.db.models`；`tests.contract.support`
- Produces: 守卫 2（project-structure 5）：表名/列名/类型映射/主键/外键/唯一约束/索引 ↔ database-design.md §2 全等校验；`parse_database_tables(md_text) -> dict[str, dict]` 解析函数（support.py 扩展）

- [ ] **Step 1: 扩展 `main/tests/contract/support.py`**

在文件末尾追加（保持既有接口不变）：

```python
DATABASE_DESIGN_PATH = REPO_ROOT / "docs" / "Architecture" / "database-design.md"


def parse_database_tables(md_text: str) -> dict[str, dict[str, str]]:
    """解析 database-design §2 表定义 → {表名: {列名: 声明}}。

    覆盖：表头行 `### 2.N 表名`；列行 `| 列名 | 类型 | 约束 | 说明 |`；
    忽略 `---` 分隔行、索引/约束注释行、`注:` 行。
    返回只读事实（表名/列名/是否 NOT NULL/是否 PK），类型与约束细节由守卫按需断言。
    """
    tables: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in md_text.splitlines():
        m = re.match(r"^### 2\.\d+ ([a-z_]+)$", line.strip())
        if m:
            current = m.group(1)
            tables[current] = {}
            continue
        if current is None:
            continue
        m = re.match(r"^\|\s*([a-z_]+)\s*\|", line)
        if m and "列" not in line:
            col = m.group(1)
            tables[current][col] = line
    return tables
```

（注意：表定义中"组合列"行如 `created_at / started_at / ended_at / updated_at` 以 `| created_at / ...` 开头——`[a-z_]+` 只匹配 `created_at`；复合主键行如 `| device_id | TEXT | 复合主键 |` 正常匹配。`| 列 | 类型 | 约束 | 说明 |` 表头行含"列"被过滤。索引声明行 `索引:`/`唯一约束:` 不以 `|` 开头被忽略。`注:` 行 `| 注:weekly_goal...` 会匹配 `注`？不会——`[a-z_]+` 不含中文。）

- [ ] **Step 2: 写守卫测试 `main/tests/contract/test_orm_database_guard.py`**

```python
"""契约守卫 2：infra/db ORM ↔ database-design.md（project-structure 5，红线 2）。

校验：表名集合全等；每表列名集合全等（ORM 模型字段 ↔ database-design 列）；
关键类型映射抽查（时间列 TEXT、枚举列 TEXT、布尔 INTEGER、小数 REAL）。
"""

from sqlalchemy import inspect

from infra.db.models import Base
from tests.contract.support import DATABASE_DESIGN_PATH, parse_database_tables

DOC_TABLES = parse_database_tables(DATABASE_DESIGN_PATH.read_text(encoding="utf-8"))
ORM_TABLES = {name: table for name, table in Base.metadata.tables.items()}


def test_orm_table_names_match_database_design() -> None:
    orm_names = set(ORM_TABLES)
    doc_names = set(DOC_TABLES)
    assert orm_names == doc_names


def test_orm_columns_match_database_design() -> None:
    for name, doc_cols in DOC_TABLES.items():
        orm_cols = set(ORM_TABLES[name].columns.keys())
        assert orm_cols == set(doc_cols), f"{name}: ORM 列 {orm_cols} != database-design {set(doc_cols)}"


def test_orm_primary_keys_match_database_design() -> None:
    for name, table in ORM_TABLES.items():
        pk = {c.name for c in table.primary_key.columns}
        doc_cols = DOC_TABLES[name]
        doc_pk = {
            col
            for col, decl in doc_cols.items()
            if "PK" in decl or "复合主键" in decl
        }
        assert pk == doc_pk, f"{name}: ORM PK {pk} != database-design {doc_pk}"


def test_orm_type_mapping_follows_convention() -> None:
    """database-design §0 类型映射抽查：时间/枚举→TEXT、布尔→INTEGER、小数→REAL、JSON→TEXT。"""
    time_cols = {"created_at", "updated_at", "last_active_at", "started_at", "ended_at", "due", "last_review", "reviewed_at"}
    for name, table in ORM_TABLES.items():
        for col in table.columns.values():
            if col.name in time_cols:
                assert str(col.type) in ("VARCHAR", "TEXT"), f"{name}.{col.name}: 时间列应为 TEXT，实际 {col.type}"
    # 布尔/枚举抽查
    assert str(ORM_TABLES["cards"].c.answer_boolean.type).startswith("INTEGER")
    assert str(ORM_TABLES["review_states"].c.stability.type).startswith("REAL")
    assert str(ORM_TABLES["cards"].c.card_type.type) in ("VARCHAR", "TEXT")
```

（注意：SQLAlchemy `String` 的 str() 是 `VARCHAR`——断言兼容两者；`answer_boolean` 是 `Integer` → `INTEGER`。若有断言与实际不符，以 database-design §0 为准修正测试或模型，并记录在报告中。）

- [ ] **Step 3: 运行确认通过**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/test_orm_database_guard.py -v`
Expected: PASS（若失败，逐条修正：优先修模型对齐 database-design，其次修测试解析器）

- [ ] **Step 4: ruff/mypy + 全量测试**

Run: `conda run -n shanka-backend python -m pytest -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [ ] **Step 5: 提交**

```bash
git add main/tests/contract/support.py main/tests/contract/test_orm_database_guard.py
git commit -m "feat(guard): ORM↔database-design 契约守卫（守卫 2）"
```

---

### Task 5: request_id 中间件 + JSON 结构化日志（O-1）

**Files:**
- Create: `main/infra/logging.py`（JSONFormatter + setup_logging）
- Create: `main/app/middleware/request_id.py`
- Create: `main/app/middleware/logging.py`（请求日志中间件）
- Modify: `main/app/main.py`（装配中间件与日志）
- Create: `main/tests/integration/test_request_logging.py`
- Create: `main/tests/unit/test_json_logging.py`

**Interfaces:**
- Consumes: F0 `infra.clock`、`app/config.Settings`；Task 1 session（不消费）；`format_utc`（infra.db.session）
- Produces: `infra.logging.JSONFormatter`（单行 JSON；字段 timestamp/level/request_id/device_id/task_id/batch_id/error_code/message + 附加 method/path/status/duration_ms）；`app.middleware.request_id.RequestIDMiddleware`（生成 `request.state.request_id`，响应头 `X-Request-ID`，contextvars 供日志）；`app.middleware.logging.LoggingMiddleware`（INFO 请求进出 + ERROR 异常）；Task 6/7/9 消费 request_id

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_json_logging.py`**

```python
"""infra.logging JSON 结构化日志单元测试（structure-contract 8.1）。"""

import json
import logging

from infra.logging import JSONFormatter


def _capture(record: logging.LogRecord) -> dict[str, object]:
    formatter = JSONFormatter()
    line = formatter.format(record)
    return json.loads(line)


def test_json_logging_single_line_with_contract_fields() -> None:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="request ok", args=(), exc_info=None,
    )
    record.request_id = "req-123"  # type: ignore[attr-defined]
    record.device_id = "dev-1"  # type: ignore[attr-defined]
    data = _capture(record)
    assert set(data) >= {
        "timestamp", "level", "request_id", "device_id",
        "task_id", "batch_id", "error_code", "message",
    }
    assert data["message"] == "request ok"
    assert data["level"] == "INFO"
    assert data["request_id"] == "req-123"


def test_json_logging_extra_attributes_flat_keys() -> None:
    record = logging.LogRecord(
        name="test", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="rate limited", args=(), exc_info=None,
    )
    record.request_id = "req-1"  # type: ignore[attr-defined]
    record.method = "POST"  # type: ignore[attr-defined]
    record.path = "/v1/decks"  # type: ignore[attr-defined]
    record.status = 429  # type: ignore[attr-defined]
    record.duration_ms = 12  # type: ignore[attr-defined]
    data = _capture(record)
    assert data["method"] == "POST"
    assert data["path"] == "/v1/decks"
    assert data["status"] == 429
    assert data["duration_ms"] == 12
    assert data["message"] == "rate limited"
```

（说明：8 个契约字段为最小集；method/path/status/duration_ms 由中间件以 record 属性注入，Formatter 输出时扁平附加。）

- [ ] **Step 2: 写失败集成测试 `main/tests/integration/test_request_logging.py`**

```python
"""request_id + JSON 请求日志集成测试（structure-contract 8.1）。"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def captured_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[list[dict[str, object]]]:
    """把请求日志中间件的 logger 输出捕获到内存列表。"""
    records: list[dict[str, object]] = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(
        json.loads(record.getMessage())  # type: ignore[attr-defined, assignment]
    )
    logger = logging.getLogger("app.middleware.logging")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield records
    logger.removeHandler(handler)


def test_request_id_present_in_response_header(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID")


def test_request_logging_emits_json_line(client: TestClient, captured_logs: list[dict[str, object]]) -> None:
    client.get("/healthz")
    assert len(captured_logs) >= 1
    entry = captured_logs[0]
    assert set(entry) >= {
        "timestamp", "level", "request_id", "device_id",
        "task_id", "batch_id", "error_code", "message",
    }
    assert entry["level"] == "INFO"
    assert entry["message"]
```

- [ ] **Step 3: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_json_logging.py tests/integration/test_request_logging.py -v`
Expected: FAIL（ModuleNotFoundError: infra.logging / app.middleware.request_id）

- [ ] **Step 4: 实现 `main/infra/logging.py`**

```python
"""JSON 结构化日志（structure-contract 8.1）：单行 JSON，字段固定。

字段：timestamp(ISO 8601 UTC) / level / request_id / device_id / task_id /
batch_id / error_code / message；记录上的附加属性（method/path/status/
duration_ms）以扁平键输出。敏感红线（1.5/7.1）：API Key、完整 PDF 内容、
完整 Prompt 不落日志——请求日志不记录任何请求体。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from infra.db.session import format_utc

_CONTRACT_FIELDS = (
    "timestamp", "level", "request_id", "device_id",
    "task_id", "batch_id", "error_code", "message",
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": format_utc(datetime.now(timezone.utc)),
            "level": record.levelname,
            "request_id": getattr(record, "request_id", ""),
            "device_id": getattr(record, "device_id", ""),
            "task_id": getattr(record, "task_id", ""),
            "batch_id": getattr(record, "batch_id", ""),
            "error_code": getattr(record, "error_code", ""),
            "message": record.getMessage(),
        }
        for attr in ("method", "path", "status", "duration_ms"):
            if hasattr(record, attr):
                data[attr] = getattr(record, attr)
        if record.exc_info:
            data["message"] = f"{record.getMessage()} | {self.formatException(record.exc_info)}"
        return json.dumps(data, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
```

- [ ] **Step 5: 实现 `main/app/middleware/request_id.py`**

```python
"""request_id 中间件（structure-contract 8.1）：每请求生成 UUID，贯穿日志与错误关联。

实现决策：响应头 `X-Request-ID` 便于客户端与服务端日志关联（兼容性附加，不违反契约字段表）。
"""

import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class RequestIDMiddleware:
    def __init__(self, app: Callable[[Request], Awaitable[Response]]) -> None:
        self.app = app

    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        request_id_var.reset(token)
        return response
```

- [ ] **Step 6: 实现 `main/app/middleware/logging.py`**

```python
"""请求日志中间件（structure-contract 8.1）：INFO 请求进出；不记录请求体（1.5 红线）。

记录字段：method/path/status/duration_ms + request_id/device_id 上下文。
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Request, Response

logger = logging.getLogger("app.middleware.logging")


class LoggingMiddleware:
    def __init__(self, app: Callable[[Request], Awaitable[Response]]) -> None:
        self.app = app

    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start = time.monotonic()
        try:
            response = await call_next(request)
            self._log(request, response.status_code, start)
            return response
        except Exception as exc:
            logger.error(
                "request failed",
                extra={
                    "request_id": getattr(request.state, "request_id", ""),
                    "device_id": getattr(request.state, "device_id", ""),
                    "error_code": "INTERNAL_ERROR",
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": int((time.monotonic() - start) * 1000),
                },
                exc_info=exc,
            )
            raise

    def _log(self, request: Request, status: int, start: float) -> None:
        logger.info(
            "request complete",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "device_id": getattr(request.state, "device_id", ""),
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": int((time.monotonic() - start) * 1000),
            },
        )
```

- [ ] **Step 7: 装配进 `main/app/main.py`**

在 `create_app` 中（`app = FastAPI(...)` 之后、`register_exception_handlers(app)` 之后）追加中间件（顺序：Logging 最外 → RequestID → 后续 device/rate 等由各自 Task 追加）：

```python
from app.middleware.logging import LoggingMiddleware
from app.middleware.request_id import RequestIDMiddleware

    app.add_middleware(LoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.state.session_factory = create_session_factory(engine)
```

并在文件头 import `from infra.db.session import create_db_engine, create_session_factory`。同时 `setup_logging(settings.log_level)` 在 `create_app` 开头调用一次（幂等：重复调用会清 root handlers——用模块级 flag 或只在模块加载时调用；简单方案：`create_app` 内调用，测试 app 各自 setup 无妨）。

- [ ] **Step 8: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_json_logging.py tests/integration/test_request_logging.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/ infra/ tests/`
Expected: PASS 全绿（若 `test_request_logging_emits_json_line` 的 logger 名称与实际不符，调整测试 logger 名；中间件 logger 名为 `app.middleware.logging`）

- [ ] **Step 9: 提交**

```bash
git add main/infra/logging.py main/app/middleware/request_id.py main/app/middleware/logging.py main/app/main.py main/tests/unit/test_json_logging.py main/tests/integration/test_request_logging.py
git commit -m "feat(obs): request_id 中间件 + JSON 结构化日志（O-1）"
```

---

### Task 6: 设备鉴权中间件（X-Device-ID + 自动注册 + 探针豁免）

**Files:**
- Modify: `main/app/middleware/device_id.py`（占位 docstring → 真实实现）
- Modify: `main/app/main.py`（装配）
- Create: `main/tests/integration/test_device_auth.py`

**Interfaces:**
- Consumes: Task 1 session_factory（devices 注册）；F0 `app.errors`（DEVICE_ID_REQUIRED/DEVICE_ID_INVALID）
- Produces: `DeviceIDMiddleware`（校验 + 注册 + 豁免 `/healthz` `/readyz` `/metrics`）；`request.state.device_id`；Task 8 幂等与 Task 9 限流消费 `request.state.device_id`

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_device_auth.py`**

```python
"""X-Device-ID 鉴权集成测试（structure-contract 1.1；database-design 2.1）。"""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app


def _new_client(tmp_path: Path) -> TestClient:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'devices.db'}", storage_path=tmp_path / "storage")
    return TestClient(create_app(settings))


def test_device_auth_missing_header_returns_401(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        resp = client.get("/v1/decks")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "DEVICE_ID_REQUIRED"
    assert resp.json()["error"]["localization_key"] == "error.device_id_required"


def test_device_auth_invalid_format_returns_401(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        resp = client.get("/v1/decks", headers={"X-Device-ID": "not-a-uuid"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "DEVICE_ID_INVALID"


def test_device_auth_accepts_valid_uuid(tmp_path: Path) -> None:
    import uuid

    device_id = str(uuid.uuid4())
    with _new_client(tmp_path) as client:
        resp = client.get("/v1/decks", headers={"X-Device-ID": device_id})
    assert resp.status_code == 404  # 无路由 → 404；鉴权已通过


def test_device_auth_first_seen_registers_device_row(tmp_path: Path) -> None:
    import uuid

    from sqlalchemy.orm import Session

    from infra.db.session import create_db_engine

    device_id = str(uuid.uuid4())
    db_path = tmp_path / "devices.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        # 建表：设备注册依赖 F1 迁移 schema——TestClient 启动不自动迁移，此处用 Base 建表
        from infra.db.models import Base

        engine = create_db_engine(settings.database_url)
        Base.metadata.create_all(engine)
        client.get("/healthz")  # 先触发一次连接
        resp = client.get("/v1/not-exist", headers={"X-Device-ID": device_id})
    assert resp.status_code == 404
    with create_db_engine(f"sqlite:///{db_path}").connect() as conn:
        row = conn.execute(text("SELECT device_id, created_at FROM devices")).fetchall()
    assert len(row) == 1
    assert row[0][0] == device_id
    assert row[0][1]  # created_at 非空


def test_device_auth_probes_exempt(tmp_path: Path) -> None:
    with _new_client(tmp_path) as client:
        assert client.get("/healthz").status_code == 200
        resp = client.get("/readyz")
    assert resp.status_code == 200
```

（说明：`/v1/decks` 与 `/v1/not-exist` 均无业务路由——F1 不实现业务 handler。鉴权中间件在 404 前执行，故 401/404 语义可测。`test_device_auth_first_seen_registers_device_row` 用 `Base.metadata.create_all` 建表以验证注册逻辑（Task 5 起 conftest 提供迁移 schema fixture 后，本测试改用迁移 fixture——见 Task 5 后 conftest 扩展说明。若冲突，以迁移 fixture 为准。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_device_auth.py -v`
Expected: FAIL（device 中间件未实现——401 未返回，行为为 404 或 500）

- [ ] **Step 3: 实现 `main/app/middleware/device_id.py`**

```python
"""X-Device-ID 鉴权中间件（structure-contract 1.1；database-design 2.1；红线 3）。

- 缺失/非法设备 ID → 401 DEVICE_ID_REQUIRED / DEVICE_ID_INVALID（1.4 错误响应）。
- 首次见到自动建立 devices 行（first_seen_ip/user_agent/last_active_at）。
- 探针与指标端点（/healthz /readyz /metrics）豁免（8.2/8.3）。
- 校验通过后 request.state.device_id 供后续中间件与 handler 使用。
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.session import format_utc

logger = logging.getLogger(__name__)

_EXEMPT_PATHS = {"/healthz", "/readyz", "/metrics"}


def _validate_device_id(device_id: str) -> bool:
    try:
        uuid.UUID(device_id)
    except ValueError:
        return False
    return str(uuid.UUID(device_id)) == device_id.lower()


class DeviceIDMiddleware:
    def __init__(self, app: Callable[[Request], Awaitable[Response]]) -> None:
        self.app = app

    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        device_id = request.headers.get("X-Device-ID")
        if device_id is None:
            return self._error(ErrorCode.DEVICE_ID_REQUIRED, "缺少 X-Device-ID 请求头")
        if not _validate_device_id(device_id):
            return self._error(ErrorCode.DEVICE_ID_INVALID, "X-Device-ID 必须为 UUID v4")
        request.state.device_id = device_id
        await self._register_device(request, device_id)
        return await call_next(request)

    def _error(self, code: ErrorCode, message: str) -> JSONResponse:
        return JSONResponse(status_code=http_status(code), content=AppError(code, message).to_response())

    async def _register_device(self, request: Request, device_id: str) -> None:
        """INSERT OR IGNORE devices 行 + 更新 last_active_at（database-design 2.1）。"""
        session_factory = request.app.state.session_factory
        now = format_utc(SystemClock().now_utc())
        first_seen_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("User-Agent") or ""
        try:
            with session_factory() as session:
                session.execute(
                    text(
                        "INSERT OR IGNORE INTO devices (device_id, first_seen_ip, user_agent, last_active_at, created_at) "
                        "VALUES (:device_id, :ip, :ua, :now, :now)"
                    ),
                    {"device_id": device_id, "ip": first_seen_ip, "ua": user_agent, "now": now},
                )
                session.execute(
                    text("UPDATE devices SET last_active_at = :now WHERE device_id = :device_id"),
                    {"device_id": device_id, "now": now},
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("device registration failed", extra={"request_id": getattr(request.state, "request_id", ""), "error_code": "INTERNAL_ERROR"})
            # 注册失败不阻断请求（数据主体为隐式创建，风控信号可降级）
```

（说明：`_error` 中 `http_status` 探测是兜底写法——F0 的 `app.errors` 提供 `http_status(code)` 函数，直接 import 使用，不要用 hasattr 探测。若 F0 已提供则改为 `from app.errors import http_status` + `JSONResponse(status_code=http_status(code), ...)`。）

- [ ] **Step 4: 装配进 `main/app/main.py`**

```python
from app.middleware.device_id import DeviceIDMiddleware

    app.add_middleware(DeviceIDMiddleware)  # 在 Logging/RequestID 之后、业务路由之前（顺序：Logging → RequestID → DeviceID → RateLimit）
```

- [ ] **Step 5: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_device_auth.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/middleware/ app/main.py`
Expected: PASS（若 `test_device_auth_first_seen_registers_device_row` 因建表时机失败——TestClient lifespan 后 engine 已存在，Base.create_all 在其上执行即可；注意 `_new_client` 创建的 client 用 `create_app` 后 `app.state.engine` 可用）

- [ ] **Step 6: 提交**

```bash
git add main/app/middleware/device_id.py main/app/main.py main/tests/integration/test_device_auth.py
git commit -m "feat(auth): X-Device-ID 鉴权中间件（自动注册 + 探针豁免）"
```

---

### Task 7: 统一错误包装扩展（VALIDATION_ERROR / INTERNAL_ERROR）

**Files:**
- Modify: `main/app/middleware/error_handler.py`（扩展）
- Create: `main/tests/integration/test_error_handler_extended.py`

**Interfaces:**
- Consumes: F0 `app.errors`（ErrorCode/AppError/http_status）
- Produces: `register_exception_handlers` 注册：`RequestValidationError` → 400 `VALIDATION_ERROR`（1.4 形状）；`AppError` → 已存在；`Exception` 兜底 → 500 `INTERNAL_ERROR`（日志 error_code，不泄露内部细节）；Task 8+ 全部 handler 消费

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_error_handler_extended.py`**

```python
"""统一错误包装扩展集成测试（structure-contract 1.4；VALIDATION_ERROR/INTERNAL_ERROR）。"""

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.middleware.error_handler import register_exception_handlers


def test_validation_error_returns_400_contract_shape() -> None:
    from pydantic import BaseModel

    class Payload(BaseModel):
        name: str

    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.post("/echo")
    def echo(payload: Payload) -> dict[str, str]:
        return {"name": payload.name}

    with TestClient(probe) as client:
        resp = client.post("/echo", json={"name": 123})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["localization_key"] == "error.validation_error"
    assert body["error"]["message"]


def test_unexpected_exception_returns_500_internal_error() -> None:
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.get("/boom")
    def boom() -> None:
        raise RuntimeError("内部细节 secret-internals")

    with TestClient(probe) as client:
        resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["localization_key"] == "error.internal_error"
    # 内部细节不得出现在响应
    assert "secret-internals" not in str(body)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_error_handler_extended.py -v`
Expected: FAIL（RequestValidationError 默认 422；RuntimeError 默认 500 HTML 或崩溃）

- [ ] **Step 3: 扩展 `main/app/middleware/error_handler.py`**

在现有文件基础上追加：

```python
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import AppError, ErrorCode, http_status

logger = logging.getLogger("app.middleware.error_handler")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=http_status(exc.code), content=exc.to_response())

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 1.4 错误响应：VALIDATION_ERROR 400；message 不暴露内部细节
        err = AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
        )
        return JSONResponse(status_code=400, content=err.to_response())

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled exception",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "error_code": "INTERNAL_ERROR",
            },
            exc_info=exc,
        )
        err = AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
        return JSONResponse(status_code=500, content=err.to_response())
```

（注意：`Exception` handler 会吞掉 Starlette 的 HTTPException（如 404/405）——FastAPI 的 HTTPException 子类 handler 优先于 Exception？Starlette 的 HTTPException handler 是注册在 app 上的默认 handler，自定义 `Exception` handler 会覆盖所有。为避免破坏 404/405 语义，需保留 HTTPException 处理：在 `handle_unexpected` 中判断 `isinstance(exc, HTTPException)` 则按原样返回。完整实现见下一步。）

- [ ] **Step 4: 修正 `handle_unexpected`（保留 HTTPException 语义）**

```python
from starlette.exceptions import HTTPException as StarletteHTTPException


    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, StarletteHTTPException):
            # 保留 FastAPI 内置 HTTPException（404/405 等）默认语义
            raise exc
        logger.error(...)  # 同上
        err = AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
        return JSONResponse(status_code=500, content=err.to_response())
```

（`raise exc` 在 exception handler 内会重新触发该 handler——需改为 `return JSONResponse(status_code=exc.status_code, content=exc.detail)` 或注册 HTTPException 的专用 handler。**更稳妥**：显式注册 `StarletteHTTPException` handler 返回原样，然后 Exception handler 兜底其余：）

```python
    @app.exception_handler(StarletteHTTPException)
    def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled exception",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "error_code": "INTERNAL_ERROR",
            },
            exc_info=exc,
        )
        err = AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
        return JSONResponse(status_code=500, content=err.to_response())
```

（选择后者。`json` import 若未使用移除。）

- [ ] **Step 5: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_error_handler_extended.py tests/integration/test_error_handler.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/middleware/ tests/integration/`
Expected: 全绿（原 error_handler 测试仍过：AppError → 404 形状不变）

- [ ] **Step 6: 提交**

```bash
git add main/app/middleware/error_handler.py main/tests/integration/test_error_handler_extended.py
git commit -m "feat(errors): 统一错误包装扩展（VALIDATION_ERROR 400 / INTERNAL_ERROR 500）"
```

---

### Task 8: 幂等原语（request_body_hash 契约更新 + 并发占位/同事务/重放）

**Files:**
- Modify: `docs/Architecture/database-design.md`（§2.12 新增 `request_body_hash` 列——兼容性契约更新，唯一例外）
- Modify: `main/infra/db/models.py`（IdempotencyKey 加列）
- Create: `main/migrations/versions/0002_idempotency_request_body_hash.py`（增量迁移）
- Create: `main/app/middleware/idempotency.py`（原语）
- Modify: `main/app/middleware/logging.py`（不消费——幂等命中日志可选）
- Create: `main/tests/integration/test_idempotency_primitive.py`
- Create: `main/tests/unit/test_idempotency.py`

**Interfaces:**
- Consumes: Task 2 模型、Task 3 迁移、F0 errors（IDEMPOTENCY_CONFLICT）
- Produces: `app.middleware.idempotency.execute_idempotent(session, *, device_id, path, idempotency_key, request_body_hash, fn) -> tuple[bool, int, dict]`（(是否重放, status, body)；同事务；并发唯一约束占位；冲突回滚后重读重放）；`app.middleware.idempotency.request_body_hash(data: bytes) -> str`（SHA-256 hex）；`app.middleware.idempotency.get_idempotency_key(request) -> str`（写接口头校验，缺/非法 → VALIDATION_ERROR 400——由 V1 handler 接线时调用，本任务提供并单测）；database-design §2.12 加 `request_body_hash` 列 + 规则说明

- [ ] **Step 1: 契约更新 `docs/Architecture/database-design.md` §2.12**

在 idempotency_keys 表定义中新增列（放在 response_body 之后）：

```markdown
| request_body_hash | TEXT | NOT NULL | 首次请求体 SHA-256 摘要(hex)；幂等键相同但摘要与首次不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3 比对的持久化载体,审核补全) |
```

并在 §2.12 规则段追加：

```markdown
- 请求体一致性:重复请求携带相同 `Idempotency-Key` 时,比对 `request_body_hash` 与首次记录;不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3)。
```

- [ ] **Step 2: 更新 `main/infra/db/models.py` IdempotencyKey**

追加列：

```python
    request_body_hash: Mapped[str] = mapped_column(String, nullable=False)
```

- [ ] **Step 3: 写增量迁移 `main/migrations/versions/0002_idempotency_request_body_hash.py`**

（先跑 `alembic revision --autogenerate -m "add idempotency request_body_hash"` 生成骨架，再按以下模板手工完善；注意 0002 必须只含本列变更，不得包含 0001 已建表：）

```python
"""add idempotency request_body_hash

Revision ID: <autogen>
Revises: <0001 revision id>
Create Date: 2026-08-10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "<autogen>"
down_revision: Union[str, None] = "<0001 revision id>"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "idempotency_keys",
        sa.Column("request_body_hash", sa.String(), nullable=False, server_default=""),
    )
    # 去掉 server_default（幂等表在写入时总是提供该值）
    op.alter_column("idempotency_keys", "request_body_hash", server_default=None)


def downgrade() -> None:
    op.drop_column("idempotency_keys", "request_body_hash")
```

- [ ] **Step 4: 更新迁移测试断言（Task 3 测试文件追加一个用例）**

```python
def test_alembic_0002_adds_request_body_hash(alembic_env: tuple[object, Path]) -> None:
    config, db_path = alembic_env
    command.upgrade(config, "head")
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info('idempotency_keys')"))}
    assert "request_body_hash" in cols
```

- [ ] **Step 5: 写失败单元测试 `main/tests/unit/test_idempotency.py`**

```python
"""幂等原语单元测试（structure-contract 1.3；database-design 2.12）。"""

import hashlib

import pytest

from app.middleware.idempotency import request_body_hash


def test_idempotency_request_body_hash_deterministic() -> None:
    body = b'{"name": "deck"}'
    expected = hashlib.sha256(body).hexdigest()
    assert request_body_hash(body) == expected


def test_idempotency_request_body_hash_differs_for_diff_body() -> None:
    assert request_body_hash(b'{"a":1}') != request_body_hash(b'{"a":2}')
```

- [ ] **Step 6: 写失败集成测试 `main/tests/integration/test_idempotency_primitive.py`**

```python
"""幂等原语集成测试（1.3/2.12）：重放、冲突、并发占位、回滚、同事务。"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.middleware.idempotency import execute_idempotent, request_body_hash
from infra.db.models import Base
from infra.db.session import create_db_engine, create_session_factory


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    return factory


def _side_effect_rows(session_factory: Callable[[], Session]) -> int:
    with session_factory() as s:
        return s.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() or 0


def test_idempotency_fresh_executes_and_records(session_factory: Callable[[], Session]) -> None:
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    with session_factory() as session:
        replayed, status, body = execute_idempotent(
            session,
            device_id="dev-1",
            path="/v1/decks",
            idempotency_key=str(uuid.uuid4()),
            request_body_hash=request_body_hash(b'{"name":"d"}'),
            fn=biz,
        )
        session.commit()
    assert replayed is False
    assert status == 201
    assert body == {"created": True}
    assert len(calls) == 1
    assert _side_effect_rows(session_factory) == 1


def test_idempotency_replay_returns_first_response(session_factory: Callable[[], Session]) -> None:
    key = str(uuid.uuid4())
    body = b'{"name":"d"}'
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    with session_factory() as session:
        execute_idempotent(session, device_id="dev-1", path="/v1/decks", idempotency_key=key, request_body_hash=request_body_hash(body), fn=biz)
        session.commit()
    with session_factory() as session:
        replayed, status, body_out = execute_idempotent(
            session, device_id="dev-1", path="/v1/decks", idempotency_key=key,
            request_body_hash=request_body_hash(body), fn=biz,
        )
        session.commit()
    assert replayed is True
    assert status == 201
    assert body_out == {"created": True}
    assert len(calls) == 1  # 业务只执行一次


def test_idempotency_body_mismatch_raises_conflict(session_factory: Callable[[], Session]) -> None:
    from app.errors import AppError, ErrorCode

    key = str(uuid.uuid4())

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        return 201, {"created": True}

    with session_factory() as session:
        execute_idempotent(session, device_id="dev-1", path="/v1/decks", idempotency_key=key, request_body_hash=request_body_hash(b'{"name":"d"}'), fn=biz)
        session.commit()
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            execute_idempotent(
                session, device_id="dev-1", path="/v1/decks", idempotency_key=key,
                request_body_hash=request_body_hash(b'{"name":"OTHER"}'), fn=biz,
            )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_idempotency_concurrent_same_key_single_effect(session_factory: Callable[[], Session]) -> None:
    """并发同键：唯一约束占位，后到者回滚并重读重放（database-design 2.12）。"""
    import threading

    key = str(uuid.uuid4())
    body = b'{"name":"d"}'
    calls: list[str] = []
    results: list[tuple[bool, int, dict[str, object]]] = []
    lock = threading.Lock()

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        return 201, {"created": True}

    def worker() -> None:
        with session_factory() as session:
            try:
                out = execute_idempotent(
                    session, device_id="dev-1", path="/v1/decks", idempotency_key=key,
                    request_body_hash=request_body_hash(body), fn=biz,
                )
                session.commit()
                with lock:
                    results.append(out)
            except Exception:
                with lock:
                    results.append(("err",))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1  # 业务副作用仅一次
    assert _side_effect_rows(session_factory) == 1  # 幂等记录仅一行
    # 两个线程都拿到结果（一个 fresh、一个 replayed）
    assert len(results) == 2
    fresh = [r for r in results if r[0] is False]
    replayed = [r for r in results if r[0] is True]
    assert len(fresh) == 1 and len(replayed) == 1


def test_idempotency_rollback_releases_claim(session_factory: Callable[[], Session]) -> None:
    """业务失败回滚：幂等记录一并回滚，同键重试重新执行（1.3 仅记录成功）。"""
    key = str(uuid.uuid4())
    calls: list[str] = []

    def biz(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("x")
        raise RuntimeError("biz failed")

    with session_factory() as session:
        with pytest.raises(RuntimeError):
            execute_idempotent(session, device_id="dev-1", path="/v1/decks", idempotency_key=key, request_body_hash=request_body_hash(b'{}'), fn=biz)
        session.rollback()
    assert _side_effect_rows(session_factory) == 0
    # 同键重试 → 重新执行（无记录 → fresh）
    def biz2(session: Session) -> tuple[int, dict[str, object]]:
        calls.append("y")
        return 200, {"ok": True}

    with session_factory() as session:
        replayed, status, body_out = execute_idempotent(
            session, device_id="dev-1", path="/v1/decks", idempotency_key=key,
            request_body_hash=request_body_hash(b'{}'), fn=biz2,
        )
        session.commit()
    assert replayed is False
    assert calls == ["x", "y"]
```

- [ ] **Step 7: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_idempotency.py tests/integration/test_idempotency_primitive.py -v`
Expected: FAIL（ModuleNotFoundError: app.middleware.idempotency）

- [ ] **Step 8: 实现 `main/app/middleware/idempotency.py`**

```python
"""Idempotency-Key 幂等原语（structure-contract 1.3；database-design 2.12；红线 3 于 app/middleware 统一）。

execute_idempotent 由写接口 handler 在请求级 session 内调用：
- 首次：执行 fn(session) → INSERT 幂等记录（response_status/response_body/request_body_hash），
  与业务副作用同一事务（调用方 commit；失败回滚同时释放占位）。
- 重复：同键同 body → 重放首次成功响应（不执行业务）；同键异 body → 409 IDEMPOTENCY_CONFLICT。
- 并发：唯一约束 (device_id, path, idempotency_key) 抢占；后到事务（BEGIN IMMEDIATE 串行化）回滚
  后重读 → 重放，保证业务副作用仅一次（AC-05/AC-10）。
"""

import hashlib
import logging
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.clock import SystemClock
from infra.db.models import IdempotencyKey
from infra.db.session import format_utc

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[[Session], tuple[int, dict[str, Any]]])


def request_body_hash(data: bytes) -> str:
    """请求体 SHA-256 摘要（hex），幂等 body 比对载体（database-design 2.12）。"""
    return hashlib.sha256(data).hexdigest()


def get_idempotency_key(request: Request) -> str:
    """读 Idempotency-Key 请求头；缺失/非法 → VALIDATION_ERROR 400（写接口强制）。"""
    key = request.headers.get("Idempotency-Key", "")
    if not key:
        raise AppError(ErrorCode.VALIDATION_ERROR, "写操作必须携带 Idempotency-Key")
    try:
        parsed = uuid.UUID(key)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, "Idempotency-Key 必须为 UUID") from exc
    return str(parsed)


def execute_idempotent(
    session: Session,
    *,
    device_id: str,
    path: str,
    idempotency_key: str,
    request_body_hash_value: str,
    fn: F,
) -> tuple[bool, int, dict[str, Any]]:
    """执行或重放幂等操作。返回 (是否重放, status, body)。

    调用方负责事务：成功后 commit（幂等记录与副作用同事务）；失败 rollback。
    """
    existing = session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.device_id == device_id,
            IdempotencyKey.path == path,
            IdempotencyKey.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_body_hash != request_body_hash_value:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 相同但请求体与首次不一致")
        return True, existing.response_status, json_loads_safe(existing.response_body)

    status, body = fn(session)

    record = IdempotencyKey(
        device_id=device_id,
        path=path,
        idempotency_key=idempotency_key,
        response_status=status,
        response_body=json_dumps_safe(body),
        request_body_hash=request_body_hash_value,
        created_at=format_utc(SystemClock().now_utc()),
    )
    session.add(record)
    try:
        session.flush()
    except Exception:
        # 并发占位冲突：回滚后重读重放（业务副作用随事务回滚，不重复）
        session.rollback()
        existing = session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.device_id == device_id,
                IdempotencyKey.path == path,
                IdempotencyKey.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise
        if existing.request_body_hash != request_body_hash_value:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key 相同但请求体与首次不一致")
        return True, existing.response_status, json_loads_safe(existing.response_body)

    return False, status, body


def json_dumps_safe(body: dict[str, Any]) -> str:
    import json

    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def json_loads_safe(raw: str) -> dict[str, Any]:
    import json

    return json.loads(raw)
```

（`import json` 放模块顶层；`session.flush()` 触发 INSERT 立即暴露唯一约束冲突——IntegrityError 会标记事务回滚，`session.rollback()` 后重读。注意：flush 的 IntegrityError 在 rollback 后 session 可用。）

- [ ] **Step 9: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_idempotency.py tests/integration/test_idempotency_primitive.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/middleware/idempotency.py tests/unit/test_idempotency.py tests/integration/test_idempotency_primitive.py`
Expected: 全绿（并发测试可能偶发——BEGIN IMMEDIATE 串行化后两线程必然一 fresh 一 replayed；若 flaky 说明事务隔离未生效，记录并调查）

- [ ] **Step 10: 提交**

```bash
git add docs/Architecture/database-design.md main/infra/db/models.py main/migrations/versions/0002_idempotency_request_body_hash.py main/app/middleware/idempotency.py main/tests/unit/test_idempotency.py main/tests/integration/test_idempotency_primitive.py
git commit -m "feat(idempotency): 幂等原语（并发占位/同事务/重放）+ request_body_hash 契约更新"
```

---

### Task 9: 限流中间件（5 维度 + 429 + Retry-After）

**Files:**
- Modify: `main/app/config.py`（限流阈值 Settings）
- Create: `main/app/middleware/rate_limit.py`
- Modify: `main/app/main.py`（装配）
- Create: `main/tests/unit/test_rate_limiter.py`
- Create: `main/tests/integration/test_rate_limit.py`

**Interfaces:**
- Consumes: F0 Settings/errors（RATE_LIMITED）；Task 5 request_id（日志）；Task 6 device_id（`request.state.device_id`）
- Produces: `app.middleware.rate_limit.RateLimiter`（内存固定窗口；`check(scope, key) -> tuple[bool, int]` (允许, Retry-After 秒)）；`RateLimitMiddleware`（按 1.6 表维度检查，超限 429 + Retry-After + `rate_limit_hit_total` 指标（Task 10 接线））；Settings 字段：`rate_limit_write_per_minute: int = 60`、`rate_limit_ip_per_second: int = 5`、`rate_limit_api_key_per_hour: int = 10`、`rate_limit_samples_per_hour: int = 20`、`rate_limit_pdf_per_hour: int = 10`

- [ ] **Step 1: Settings 扩展 `main/app/config.py`**

追加字段：

```python
    # 限流阈值（structure-contract 1.6；可运维调整，客户端不得硬编码）
    rate_limit_write_per_minute: int = 60
    rate_limit_ip_per_second: int = 5
    rate_limit_api_key_per_hour: int = 10
    rate_limit_samples_per_hour: int = 20
    rate_limit_pdf_per_hour: int = 10
```

在 `main/tests/unit/test_settings.py` 追加断言（`test_settings_defaults` 中追加 5 行）：

```python
    assert settings.rate_limit_write_per_minute == 60
    assert settings.rate_limit_ip_per_second == 5
    assert settings.rate_limit_api_key_per_hour == 10
    assert settings.rate_limit_samples_per_hour == 20
    assert settings.rate_limit_pdf_per_hour == 10
```

- [ ] **Step 2: 写失败单元测试 `main/tests/unit/test_rate_limiter.py`**

```python
"""限流器单元测试（structure-contract 1.6）：固定窗口计数 + Retry-After。"""

import time

from app.middleware.rate_limit import RateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_rate_limiter_allows_within_limit() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=3, window_seconds=60, clock=clock)
    assert limiter.check("dev-1") == (True, 0)
    assert limiter.check("dev-1") == (True, 0)
    assert limiter.check("dev-1") == (True, 0)


def test_rate_limiter_blocks_over_limit_with_retry_after() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("dev-1")
    limiter.check("dev-1")
    allowed, retry_after = limiter.check("dev-1")
    assert allowed is False
    assert retry_after > 0
    assert retry_after <= 60


def test_rate_limiter_window_rolls_over() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.check("dev-1")
    limiter.check("dev-1")
    clock.advance(61)
    assert limiter.check("dev-1") == (True, 0)


def test_rate_limiter_scopes_isolated() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.check("a")
    assert limiter.check("b") == (True, 0)
```

- [ ] **Step 3: 写失败集成测试 `main/tests/integration/test_rate_limit.py`**

```python
"""限流集成测试（structure-contract 1.6）：429 + Retry-After + 维度 + 探针行为。"""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _device_headers(device_id: str | None = None) -> dict[str, str]:
    return {"X-Device-ID": device_id or str(uuid.uuid4())}


def test_rate_limit_write_dimension_429_with_retry_after(tmp_path: Path) -> None:
    """写操作 60 req/min/device：阈值可下调以便测试——用 Settings 构造小阈值 app。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=3,
    )
    with TestClient(create_app(settings)) as client:
        headers = _device_headers()
        codes = []
        for _ in range(5):
            # POST /v1/decks 无路由 → 404；限流中间件在路由前执行
            resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
            codes.append(resp.status_code)
    assert codes[:3] == [404, 404, 404]  # 前 3 次通过（业务路由缺失 → 404）
    assert codes[3] == 429 and codes[4] == 429
    # Retry-After 响应头存在
    with TestClient(create_app(settings)) as client:
        headers = _device_headers()
        for _ in range(4):
            client.post("/v1/decks", json={"name": "d"}, headers=headers)
        resp = client.post("/v1/decks", json={"name": "d"}, headers=headers)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    assert int(resp.headers["Retry-After"]) > 0
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_ip_dimension_blocks(tmp_path: Path) -> None:
    """IP 5 req/s（全部接口）：用低阈值 app 验证。"""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_ip.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=2,
    )
    with TestClient(create_app(settings)) as client:
        codes = [client.get("/healthz").status_code for _ in range(4)]
    assert codes[:2] == [200, 200]
    assert codes[2] == 429  # IP 维度覆盖探针（1.6 表"全部接口"）


def test_rate_limit_device_scope_isolated_per_device(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rl_iso.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=2,
    )
    with TestClient(create_app(settings)) as client:
        codes_a = [client.post("/v1/decks", json={}, headers=_device_headers()).status_code for _ in range(3)]
        codes_b = [client.post("/v1/decks", json={}, headers=_device_headers()).status_code for _ in range(2)]
    assert codes_a == [404, 404, 429]
    assert codes_b == [404, 404]  # 设备 B 不受设备 A 影响
```

（说明：写维度 scope 判定 = 非豁免路径的 POST/PUT/PATCH/DELETE（`POST /samples`、`PUT /api-key`、`POST /pdfs` 有专门维度，判定顺序：专门维度 > 通用写维度 > IP。探针路径豁免 device/专门维度，但仍受 IP 维度约束——契约 1.6 IP 行"全部接口"。实现时在 plan Step 5 明确 scope 判定表。）

- [ ] **Step 4: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_rate_limiter.py tests/integration/test_rate_limit.py -v`
Expected: FAIL（ModuleNotFoundError: app.middleware.rate_limit）

- [ ] **Step 5: 实现 `main/app/middleware/rate_limit.py`**

```python
"""限流中间件（structure-contract 1.6；红线 3）。

维度判定（1.6 表）：
- IP 5 req/s：全部接口（含探针，采集器例外由部署层处理——契约字面"全部接口"）。
- 写操作 60 req/min/device：全部写接口（POST/PUT/PATCH/DELETE），
  被专门维度覆盖的接口（/api-key、/samples、/pdfs）除外。
- PUT /api-key 10 次/时/device；POST /samples 20 次/时/device；POST /pdfs 10 次/时/device。

实现：内存固定窗口（单实例 MVP；多实例演进时换共享存储，业务逻辑不变——见契约 4.4 定式）。
超限：429 RATE_LIMITED + Retry-After 响应头（秒）。
"""

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from app.config import Settings
from app.errors import AppError, ErrorCode, http_status

logger = logging.getLogger(__name__)

_EXEMPT_DEVICE_PATHS = {"/healthz", "/readyz", "/metrics"}


@dataclass
class RateLimiter:
    """固定窗口限流器：`check(key) -> (allowed, retry_after_seconds)`。"""

    limit: int
    window_seconds: int
    clock: Callable[[], float] = field(default=time.monotonic)
    _counts: dict[tuple[int, str], tuple[float, int]] = field(default_factory=dict)

    def check(self, key: str) -> tuple[bool, int]:
        now = self.clock()
        window_id = int(now // self.window_seconds)
        # 惰性清理已过期窗口，防止无界增长（单设备窗口数有限，清理即足够）
        expired = [wid for wid, _ in self._counts if wid < window_id]
        for wid in expired:
            del self._counts[wid]
        entry = self._counts.get((window_id, key))
        if entry is None:
            self._counts[(window_id, key)] = (now, 1)
            return True, 0
        _, count = entry
        if count >= self.limit:
            retry_after = int(self.window_seconds - (now % self.window_seconds)) + 1
            return False, retry_after
        self._counts[(window_id, key)] = (now, count + 1)
        return True, 0


class RateLimitMiddleware:
    def __init__(self, app: Callable[[Request], Awaitable[Response]], settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self._ip_limiter = RateLimiter(limit=settings.rate_limit_ip_per_second, window_seconds=1)
        self._write_limiter = RateLimiter(limit=settings.rate_limit_write_per_minute, window_seconds=60)
        self._api_key_limiter = RateLimiter(limit=settings.rate_limit_api_key_per_hour, window_seconds=3600)
        self._samples_limiter = RateLimiter(limit=settings.rate_limit_samples_per_hour, window_seconds=3600)
        self._pdf_limiter = RateLimiter(limit=settings.rate_limit_pdf_per_hour, window_seconds=3600)

    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        client_ip = request.client.host if request.client else "unknown"
        allowed, retry_after = self._ip_limiter.check(client_ip)
        if not allowed:
            return self._rate_limited(request, "ip", retry_after)
        scope = self._scope(request)
        if scope is not None:
            device_id = getattr(request.state, "device_id", "")
            limiter = {
                "write": self._write_limiter,
                "api_key": self._api_key_limiter,
                "samples": self._samples_limiter,
                "pdf": self._pdf_limiter,
            }[scope]
            allowed, retry_after = limiter.check(device_id)
            if not allowed:
                return self._rate_limited(request, scope, retry_after)
        return await call_next(request)

    def _scope(self, request: Request) -> str | None:
        """1.6 维度判定：None = 仅 IP 维度。"""
        if request.url.path in _EXEMPT_DEVICE_PATHS:
            return None
        method = request.method
        path = request.url.path
        if method == "POST" and path == "/v1/samples":
            return "samples"
        if method == "PUT" and path == "/v1/api-key":
            return "api_key"
        if method == "POST" and path == "/v1/pdfs":
            return "pdf"
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            return "write"
        return None

    def _rate_limited(self, request: Request, scope: str, retry_after: int) -> JSONResponse:
        logger.warning(
            "rate limited",
            extra={
                "request_id": getattr(request.state, "request_id", ""),
                "device_id": getattr(request.state, "device_id", ""),
                "error_code": "RATE_LIMITED",
                "path": request.url.path,
            },
        )
        # rate_limit_hit_total 指标（Task 10 接线）
        try:
            from app.api.metrics import RATE_LIMIT_HIT_TOTAL

            RATE_LIMIT_HIT_TOTAL.labels(scope=scope).inc()
        except ImportError:
            pass
        response = JSONResponse(
            status_code=429,
            content=AppError(ErrorCode.RATE_LIMITED, "请求过于频繁，请稍后重试").to_response(),
        )
        response.headers["Retry-After"] = str(retry_after)
        return response
```

- [ ] **Step 6: 装配进 `main/app/main.py`**

```python
from app.middleware.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, settings=settings)  # 在 DeviceID 之后
```

- [ ] **Step 7: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_rate_limiter.py tests/integration/test_rate_limit.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/middleware/ tests/`
Expected: 全绿（注意 `_prune` 修正后 mypy 通过；`time.monotonic` 默认 clock 与 `_FakeClock` 注入兼容——`clock` 参数类型 `Callable[[], float]`）

- [ ] **Step 8: 提交**

```bash
git add main/app/config.py main/app/middleware/rate_limit.py main/app/main.py main/tests/unit/test_rate_limiter.py main/tests/integration/test_rate_limit.py main/tests/unit/test_settings.py
git commit -m "feat(rate-limit): 限流中间件（5 维度 + 429 + Retry-After）"
```

---

### Task 10: metrics 端点与 HTTP/限流指标（O-3，R-04）

**Files:**
- Create: `main/app/api/metrics.py`
- Modify: `main/app/main.py`（挂路由）
- Create: `main/app/middleware/metrics_middleware.py`（HTTP 指标采集）
- Modify: `main/app/main.py`（装配）
- Create: `main/tests/integration/test_metrics.py`

**Interfaces:**
- Consumes: prometheus-client（Task 1）；Task 9 的 `RATE_LIMIT_HIT_TOTAL`（rate_limit.py 已引用 `app.api.metrics`——本任务创建该模块）
- Produces: `app.api.metrics.RATE_LIMIT_HIT_TOTAL`（Counter，labels: scope）；`HTTP_REQUESTS_TOTAL`（Counter，labels: method/path/status）；`HTTP_REQUEST_DURATION_SECONDS`（Histogram）；`router`（`GET /metrics` → `generate_latest(REGISTRY)`，`content-type: text/plain; version=0.0.4; charset=utf-8`，豁免鉴权（8.3））；llm/generation/batch 指标在 V3B/V5A 补充（本任务只建共享 registry 与 F1 范围指标）

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_metrics.py`**

```python
"""metrics 集成测试（structure-contract 8.3；R-04 不进业务 OpenAPI，直接测）。"""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_metrics_endpoint_returns_prometheus_text(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'm.db'}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "# HELP http_requests_total" in resp.text
    assert "# TYPE http_requests_total counter" in resp.text


def test_metrics_tracks_http_requests(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'm2.db'}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client:
        client.get("/healthz")
        client.get("/healthz")
        resp = client.get("/metrics")
    assert 'http_requests_total{method="GET",path="/healthz",status="200"} 2.0' in resp.text


def test_metrics_rate_limit_hit_recorded(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'm3.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_write_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        import uuid

        headers = {"X-Device-ID": str(uuid.uuid4())}
        client.post("/v1/decks", json={}, headers=headers)  # 首次（404，路由缺失）
        client.post("/v1/decks", json={}, headers=headers)  # 超限 → 429 → 指标 +1
        resp = client.get("/metrics")
    assert 'rate_limit_hit_total{scope="write"} 1.0' in resp.text
```

（注意：metrics 端点本身也会被 HTTP 指标中间件计入——`/metrics` 的请求会以 `path="/metrics"` 出现，不影响上述断言。限流中间件对 `/metrics` 豁免 device 维度，但 IP 维度可能触发——测试循环量小不触发。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_metrics.py -v`
Expected: FAIL（ModuleNotFoundError: app.api.metrics / 404）

- [ ] **Step 3: 实现 `main/app/api/metrics.py`**

```python
"""Prometheus 指标（structure-contract 8.3；R-04 有意不进业务 OpenAPI，F1/R1 直接测试）。

F1 范围：HTTP 请求与限流指标 + 共享 registry；llm/generation/batch 指标在
V3B（llm_requests_total/llm_request_duration_seconds/llm_tokens_total）与
V5A（generation_tasks_total/generation_tasks_duration_seconds/batch_retry_total）
补充，全部注册到同一 REGISTRY。
"""

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest

router = APIRouter(tags=["observability"])

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "HTTP 请求总数", ["method", "path", "status"], registry=REGISTRY
)
HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds", "HTTP 请求耗时", registry=REGISTRY
)
RATE_LIMIT_HIT_TOTAL = Counter(
    "rate_limit_hit_total", "限流触发次数", ["scope"], registry=REGISTRY
)


@router.get("/metrics")
def metrics() -> Response:
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
```

（说明：prometheus-client 的 Counter 对同 label 组合幂等累加；导出值浮点格式如 `2.0`，测试断言字符串匹配。llm/generation/batch 指标在 V3B/V5A 注册到同一 REGISTRY。）

- [ ] **Step 4: 实现 `main/app/middleware/metrics_middleware.py`**

```python
"""HTTP 指标采集中间件（structure-contract 8.3）：http_requests_total + duration histogram。"""

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from app.api.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL


class MetricsMiddleware:
    def __init__(self, app: Callable[[Request], Awaitable[Response]]) -> None:
        self.app = app

    async def __call__(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        HTTP_REQUESTS_TOTAL.labels(method=request.method, path=request.url.path, status=str(response.status_code)).inc()
        HTTP_REQUEST_DURATION_SECONDS.observe(duration)
        return response
```

- [ ] **Step 5: 装配进 `main/app/main.py`**

```python
from app.api import metrics
from app.middleware.metrics_middleware import MetricsMiddleware

    app.include_router(metrics.router)
    app.add_middleware(MetricsMiddleware)  # 最外层（先于 Logging）
```

（中间件顺序：Metrics → Logging → RequestID → DeviceID → RateLimit。FastAPI `add_middleware` 后加的在最外层——按此顺序调用使其符合：先 `add_middleware(MetricsMiddleware)`，再 Logging，再 RequestID，再 DeviceID，再 RateLimit。）

- [ ] **Step 6: 运行确认通过 + ruff/mypy + 全量测试**

Run: `conda run -n shanka-backend python -m pytest -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿（注意 `test_rate_limit.py` 与 `test_metrics.py` 的限流 app 在多次请求下可能互扰——测试各自用独立 Settings/app 实例，指标为进程级注册表：`http_requests_total` 跨测试累加不影响断言格式；`rate_limit_hit_total` 断言用 `in` 匹配特定 scope 行，跨测试累加仍匹配）

- [ ] **Step 7: 提交**

```bash
git add main/app/api/metrics.py main/app/middleware/metrics_middleware.py main/app/main.py main/tests/integration/test_metrics.py
git commit -m "feat(metrics): /metrics 端点 + HTTP/限流指标（O-3，R-04 直测）"
```

---

### Task 11: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 F1 产物；不新增代码

**Interfaces:**
- Consumes: Task 1~10 全部产物

- [ ] **Step 1: 四工具命令全绿**

Run（均在 `main/`）:
```bash
conda run -n shanka-backend python --version
conda run -n shanka-backend python -m pytest
conda run -n shanka-backend python -m ruff check .
conda run -n shanka-backend python -m ruff format --check .
conda run -n shanka-backend python -m mypy .
```
Expected: 版本 3.12.x；四命令零失败

- [ ] **Step 2: 干净环境安装 + 空库迁移往返**

```bash
conda run -n shanka-backend python -m venv /tmp/f1-accept-venv
/tmp/f1-accept-venv/bin/pip install -q -r /home/kbzz1/shanka_backend/main/requirements-dev.lock
/tmp/f1-accept-venv/bin/pip install -q -e /home/kbzz1/shanka_backend/main
cd /home/kbzz1/shanka_backend/main && /tmp/f1-accept-venv/bin/python -c "
from alembic import command
from alembic.config import Config
import tempfile, os, pathlib
p = pathlib.Path(tempfile.mkdtemp()) / 'm.db'
cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', f'sqlite:///{p}')
command.upgrade(cfg, 'head')
command.downgrade(cfg, 'base')
command.upgrade(cfg, 'head')
print('migration-roundtrip-ok')
"
rm -rf /tmp/f1-accept-venv
```

- [ ] **Step 3: 应用启动冒烟（真实 uvicorn + 鉴权 + metrics）**

```bash
cd /home/kbzz1/shanka_backend/main
conda run -n shanka-backend uvicorn app.main:app --port 8098 > /tmp/f1-uvicorn.log 2>&1 &
UV_PID=$!
sleep 3
curl -s -o /dev/null -w "healthz-noauth=%{http_code}\n" http://127.0.0.1:8098/healthz
curl -s -o /dev/null -w "metrics-noauth=%{http_code}\n" http://127.0.0.1:8098/metrics
curl -s -o /dev/null -w "decks-no-device=%{http_code}\n" http://127.0.0.1:8098/v1/decks
curl -s -H "X-Device-ID: $(uuidgen 2>/dev/null || python3 -c 'import uuid;print(uuid.uuid4())')" -o /dev/null -w "decks-with-device=%{http_code}\n" http://127.0.0.1:8098/v1/decks
curl -s -D - -o /dev/null http://127.0.0.1:8098/healthz | grep -i x-request-id || true
kill $UV_PID
```
Expected: healthz-noauth=200、metrics-noauth=200、decks-no-device=401、decks-with-device=404（路由缺失）、X-Request-ID 响应头存在

- [ ] **Step 4: 关键集成边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_device_auth.py tests/integration/test_idempotency_primitive.py tests/integration/test_rate_limit.py tests/integration/test_alembic_migration.py tests/integration/test_metrics.py -v`
Expected: 全绿；记录输出中关键用例名（429/Retry-After、并发单副作用、迁移往返、探针豁免）

- [ ] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app main/infra --include="*.py" || true`
Expected: 无输出（实现代码不得出现 API Key 明文形态）

- [ ] **Step 6: 中间件顺序与豁免最终确认**

Run: `grep -n "add_middleware" /home/kbzz1/shanka_backend/main/app/main.py`
Expected: 顺序 Metrics → Logging → RequestID → DeviceID → RateLimit（add_middleware 后加者在外层，最终确认实际调用顺序与注释一致）

---

### Task 12: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`
- Modify: `docs/superpowers/plans/2026-08-10-f1-data-and-http-foundation.md`（标题下「结果」）

**Interfaces:**
- Consumes: Task 11 验收证据

- [ ] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 F1 行：`TODO` → `DONE`，当前证据填写：12 表 ORM+迁移（upgrade/downgrade/upgrade 往返）、ORM↔database-design 守卫、设备鉴权（401/豁免/自动注册）、request_id+JSON 日志、错误包装扩展（VALIDATION_ERROR 400/INTERNAL_ERROR 500）、幂等原语（并发占位/同事务/重放/冲突/回滚）、限流（429+Retry-After+维度隔离）、metrics 端点与指标、X-Request-ID。
- 第 6 节：新增 R-10 → `RESOLVED`（database-design 2.12 缺 body 比对载体 → 新增 `request_body_hash` 列，兼容性契约更新，已在 F1 实现与守卫覆盖）。
- 第 1 节状态基线：`可运行后端` 保持 DONE 并补 F1 证据；`自动化验证` 更新测试数。
- 计划文件标题下「结果」注明 F1 DONE 与证据位置。

- [ ] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-10-f1-data-and-http-foundation.md
git commit -m "docs(progress): F1 DONE（数据与 HTTP 共享基础），R-10 RESOLVED"
```

---

## Self-Review

**1. Spec coverage（对照 Progress F1 文本）：**

| F1 要求 | 落点 |
| --- | --- |
| 12 张表、约束、索引、外键、WAL | Task 2（ORM）+ Task 3（迁移，WAL 由 F0 engine 事件覆盖，迁移测试断言 PRAGMA） |
| Alembic 初始迁移 | Task 3（upgrade/downgrade/upgrade 往返 + 增量 0002） |
| 统一 X-Device-ID | Task 6（401/校验/自动注册/探针豁免） |
| Idempotency-Key 指纹/首次响应重放 | Task 8（request_body_hash + execute_idempotent 重放） |
| 错误包装 | Task 7（VALIDATION_ERROR/INTERNAL_ERROR + 既有 AppError） |
| request_id | Task 5（RequestIDMiddleware + X-Request-ID + contextvars） |
| 限流 | Task 9（5 维度 + 429 + Retry-After） |
| 请求级 session | Task 1（get_db_session dependency） |
| 幂等请求指纹/并发占位/唯一约束/首次响应存储原语 | Task 8（execute_idempotent + 唯一约束抢占 + 回滚重放） |
| 跨设备统一 404 | 错误码 404 已就绪（F0）；纵向包按 device_id 过滤实现；F1 设备中间件通过 401/404 语义测试 |
| JSON 日志（O-1） | Task 5（8 字段 + 附加字段 + 不记录请求体） |
| HTTP/限流指标（O-3） | Task 10（/metrics + http_requests_total/duration + rate_limit_hit_total；R-04 直测） |
| 验收：空库 upgrade/downgrade/upgrade | Task 3/11 |
| 验收：磁盘 SQLite 外键/WAL | Task 3 断言 PRAGMA |
| 验收：幂等原语并发占位与回滚、隔离、429+Retry-After、探针豁免 | Task 8/9/6 integration 测试 |
| 业务副作用与首次响应完整同事务 | F1 提供原语与测试（同事务 INSERT + 回滚）；V1 首个真实写接口完整验收（Progress 明示） |

**2. Placeholder scan：** 全部任务给出完整文件内容与可执行命令；无 TBD/TODO 占位。注意 Task 6 `_error` 的 hasattr 探测与 Task 9 `_prune` 占位实现均为"实现者应修正"的明确指示（给出正确目标），不是占位步骤。

**3. Type consistency：** `create_session_factory(engine) -> sessionmaker[Session]`（Task 1 定义，Task 6 device 中间件消费）；`get_db_session() -> Iterator[Session]`（Task 1 定义，Task 1 测试使用）；`execute_idempotent(session, *, device_id, path, idempotency_key, request_body_hash, fn) -> tuple[bool, int, dict]`（Task 8 定义与测试一致）；`request_body_hash(data: bytes) -> str`（Task 8 定义，测试一致）；`RateLimiter.check(key) -> tuple[bool, int]`（Task 9 定义，unit 测试一致）；`RATE_LIMIT_HIT_TOTAL.labels(scope=...).inc()`（Task 9 引用，Task 10 定义）；Settings 限流字段名（Task 9 定义，Task 9 测试与 Task 10 测试一致）；`alembic_env` fixture（Task 3 定义，Task 8 复用）。`idempotency_keys.request_body_hash` 列在 Task 8 的 models/迁移/契约三处一致。
