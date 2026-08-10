# V1 牌组与卡片闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**结果**：<主 Agent 整包验收通过后在此注明 V1 DONE 与证据位置>

**Goal:** 实现牌组列表/创建/详情/删除、自由刷题列表、手动新增、原子批量导入；统一 Card/position/source；真实进度查询（card_count/due_count/mastered/review_count/mastery_ratio）；删除保护（409 TASK_IN_PROGRESS）、级联及历史任务 deck_id 置空；幂等键在首个真实写接口上完整接线（业务副作用与首次响应同事务、重放、冲突、并发不双写），使 V1 依据真实验收证据标记 DONE 且 AC-09 通过。

**Architecture:** 契约驱动分层。V1 建立在 F0/F1 地基上：`app/errors.py`（错误码）、`infra/db/session.py`（session/时钟/时间格式）、`app/middleware/idempotency.py`（execute_idempotent/get_idempotency_key/request_body_hash）、`app/middleware/device_id.py`（request.state.device_id）、`tests/contract/support.py`（守卫框架）、`tests/conftest.py`。新增：`app/schemas/decks.py` + `app/schemas/cards.py`（请求/响应模型，与 openapi 一致）、`app/middleware/body_capture.py`（写操作 raw body 捕获供幂等 hash）、`services/decks/`（用例：创建/列表/详情/删除/进度聚合/删除保护）、`services/cards/`（用例：position 分配/创建/列表/导入原子/review_states 初始化）、`app/api/decks.py` + `app/api/cards.py`（handler 只做 HTTP 映射，幂等与事务由 service 用例持有）。删除保护与级联按 database-design §3；进度派生按 structure-contract 3.8/5.3（掌握判定 C-03：state=REVIEW 且 stability>=21）。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0（ORM）、pytest、ruff、mypy strict。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行（在 `main/` 下），禁止向 base/系统 Python 装依赖。
- 契约权威：openapi.yaml（/decks 与 /decks/{deck_id} 端点及 Deck/Card schema，**路径前缀 /v1 在 servers URL**）、structure-contract 3.8/3.9/5.3/6.5、database-design 2.8/2.9/2.10/§3、PRD AC-09。实现不得修改 `docs/PRD/`、`docs/Architecture/`（V1 无契约更新；R-11 的 Deck.source GENERATED 裁决不属 V1 范围，V4 处理）。
- 幂等（1.3/C-04）：V1 全部写操作（POST /decks、POST /decks/{id}/cards、POST import、DELETE /decks/{id}）走 `execute_idempotent`；业务副作用与幂等记录同一事务（调用方 commit/rollback）；重复请求重放首次 2xx；同键异 body → 409 IDEMPOTENCY_CONFLICT。
- 请求体捕获：写操作由 `app/middleware/body_capture.py` 读 raw body 存 `request.state.raw_body`（bytes），handler 用 `request_body_hash(raw_body)` 计算比对；DELETE 无 body → `b""`。**请求日志仍不记录请求体**（红线 4）。
- 跨设备统一 404（1.1）：所有资源查询按 `request.state.device_id` 过滤，无结果 → `DECK_NOT_FOUND`/`CARD_NOT_FOUND` 404，不暴露存在性。
- 删除保护（database-design §3/契约 6.5）：存在非终态任务（status IN PENDING/RUNNING/PAUSED）引用 deck_id 时 DELETE → 409 TASK_IN_PROGRESS；否则删除（cards/review_states/review_events 级联）+ `tasks.deck_id SET NULL`。
- 卡片创建同事务插入初始 review_states（database-design §3：state=NEW 初始排程参数——V1 初始值：state=NEW、stability=0.0、difficulty=0.0、due=now、reps=0、lapses=0；V2 排程器接管后按 FSRS 校准）。
- position：牌组内 max(position)+1（并发保护由 UNIQUE(deck_id, position) 兜底，冲突时整体回滚重试一次——见 Task 3）。
- version：创建时为 `format_utc(now)`（ISO 时间串，单调递增；V6 重写时递增同口径）。
- 时间格式唯一规范（database-design §0）：`YYYY-MM-DDTHH:MM:SS.sssZ`；进度聚合的 `due <= now` 用统一格式字符串比较（服务端时钟）。
- 错误响应 1.4 形状；handler 只做 HTTP 映射，禁止在 handler 暴露 ORM 对象（红线：app/schemas 视图模型转换）。
- ruff line-length 100、mypy strict；四工具命令全绿；测试命名 `test_<模块>_<行为>`；提交只 `git add` 本任务文件。
- 工作包边界：V1 不含复习评级/看板（V2）、PDF（V3A）、生成（V4+）、单卡重写（V6）；`/decks/{deck_id}/review` 端点 V2 实现，本包不注册。`app/api/` 其他占位模块（pdfs.py 等）不得改动。
- Task 1~5 由实现 subagent 完成；Task 6/7 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: Deck/Card schema 与 schema↔openapi 守卫扩展

**Files:**
- Create: `main/app/schemas/decks.py`、`main/app/schemas/cards.py`
- Modify: `main/tests/contract/support.py`（守卫扩展：type 数组/null 联合/数组/嵌套 $ref/enum）
- Create: `main/tests/contract/test_deck_card_schemas_guard.py`
- Create: `main/tests/unit/test_schemas_decks_cards.py`

**Interfaces:**
- Consumes: F0 `tests.contract.support`（check_schema_consistency、openapi_schema、resolve_ref）
- Produces: `app.schemas.decks.Deck`（响应模型：deck_id/name/source/card_count/due_count/mastered_card_count/review_count/mastery_ratio/created_at/updated_at/version）、`DeckCreate`（name: str = Field(min_length=1, max_length=64)）；`app.schemas.cards.Card`（响应模型：card_id/deck_id/source/position/front/back/card_type/version/created_at/updated_at + 可选 code/question/answer/statement/answer_boolean/explanation/generation_item_id/target_difficulty/knowledge_point_ids/evidence_score/correctness_score/difficulty_score/learning_value_score/rubric_total_score）、`CardCreate`（front/back min_length=1）、`ImportCard`（front/back min_length=1）、`ImportResult`（index/status: Literal[CREATED, FAILED]/card_id?/error?）、`ImportResponse`（results: list[ImportResult]）；Task 4 handler 与 Task 2/3 service 消费

- [ ] **Step 1: 写失败守卫测试 `main/tests/contract/test_deck_card_schemas_guard.py`**

```python
"""契约守卫：app/schemas decks/cards ↔ openapi.yaml（红线 1，守卫 1 扩展）。"""

from app.schemas.cards import Card, CardCreate, ImportResponse
from app.schemas.decks import Deck, DeckCreate
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_deck_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(Deck, openapi_schema("Deck"), load_openapi())
    assert violations == []


def test_card_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(Card, openapi_schema("Card"), load_openapi())
    assert violations == []


def test_deck_create_validates_name_bounds() -> None:
    import pydantic

    DeckCreate(name="x")
    with pydantic.ValidationError:
        DeckCreate(name="")
    with pydantic.ValidationError:
        DeckCreate(name="x" * 65)


def test_card_create_validates_front_back_nonempty() -> None:
    import pydantic

    CardCreate(front="f", back="b")
    with pydantic.ValidationError:
        CardCreate(front="", back="b")
    with pydantic.ValidationError:
        CardCreate(front="f", back="")


def test_import_response_shape() -> None:
    resp = ImportResponse(
        results=[{"index": 0, "status": "CREATED", "card_id": "11111111-1111-4111-8111-111111111111"}]
    )
    assert resp.results[0].index == 0
    assert resp.results[0].status == "CREATED"
```

（说明：`DeckCreate`/`CardCreate` 为请求模型，openapi 中无对应命名 schema（请求体内联）——守卫只校验响应模型 Deck/Card。若 `check_schema_consistency` 对 Card 的可选字段（`type: [string, 'null']` 等）报 unsupported，属预期——Task 1 Step 3 扩展守卫支持。）

- [ ] **Step 2: 写失败单元测试 `main/tests/unit/test_schemas_decks_cards.py`**

```python
"""schemas decks/cards 纯校验单元测试。"""

from app.schemas.cards import Card, CardCreate
from app.schemas.decks import Deck, DeckCreate


def test_deck_create_model() -> None:
    model = DeckCreate(name="我的牌组")
    assert model.name == "我的牌组"


def test_deck_response_model_optional_fields() -> None:
    """响应模型字段全部必填（openapi required 集合）。"""
    import pydantic

    with pydantic.ValidationError:
        Deck()  # type: ignore[call-arg]
```

（说明：响应模型字段全部必填——由守卫的 required 校验兜底，此处仅验证模型存在与构造行为。）

- [ ] **Step 3: 实现 `main/app/schemas/decks.py` 与 `main/app/schemas/cards.py`**

```python
"""decks.py：牌组请求/响应模型（openapi Deck；structure-contract 3.8 派生进度）。"""

from pydantic import BaseModel, Field


class Deck(BaseModel):
    deck_id: str
    name: str
    source: str  # MANUAL/IMPORTED（GENERATED 属 V4，R-11）
    card_count: int
    due_count: int
    mastered_card_count: int
    review_count: int
    mastery_ratio: float
    created_at: str
    updated_at: str
    version: str


class DeckCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
```

```python
"""cards.py：卡片请求/响应模型（openapi Card；structure-contract 3.9）。"""

from typing import Literal

from pydantic import BaseModel, Field


class Card(BaseModel):
    card_id: str
    deck_id: str
    source: str  # GENERATED/MANUAL/IMPORTED
    position: int
    front: str
    back: str
    code: str | None = None
    card_type: str  # QUESTION/TRUE_FALSE
    question: str | None = None
    answer: str | None = None
    statement: str | None = None
    answer_boolean: bool | None = None
    explanation: str | None = None
    generation_item_id: str | None = None
    target_difficulty: str | None = None
    knowledge_point_ids: list[str] | None = None
    evidence_score: int | None = None
    correctness_score: int | None = None
    difficulty_score: int | None = None
    learning_value_score: int | None = None
    rubric_total_score: int | None = None
    version: str
    created_at: str
    updated_at: str


class CardCreate(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class ImportCard(BaseModel):
    front: str = Field(min_length=1)
    back: str = Field(min_length=1)


class ImportResult(BaseModel):
    index: int
    status: Literal["CREATED", "FAILED"]
    card_id: str | None = None
    error: dict[str, str] | None = None


class ImportResponse(BaseModel):
    results: list[ImportResult]
```

（注意：Card 的 `source`/`card_type` 用 str 而非枚举——domain/enums 是纯领域枚举（V2+ 充实），schema 层保持 str 与 openapi 的 string enum 兼容；守卫校验 enum 值集合时若报错可扩展 `_is_enum` 处理。`answer_boolean` 为 `bool | None`（openapi `type: [boolean, 'null']`），而 ORM 列是 INTEGER——schema/ORM 转换由 service 负责。）

- [ ] **Step 4: 扩展 `main/tests/contract/support.py` 守卫**

在 `check_schema_consistency` 中支持（最小扩展，保持既有接口）：

```python
        # openapi 3.1 type 数组（如 ["string", "null"]）：取首个非 null 类型
        if isinstance(prop_type, list):
            prop_type = next((t for t in prop_type if t != "null"), None)
        if prop_type == "array":
            # items 类型映射：list[annotation] 或 list[Model]（嵌套 $ref 在 items 中）
            items = resolved.get("items", {})
            item_schema = resolve_ref(items, openapi)
            if item_schema.get("type") == "object":
                item_origin = getattr(annotation, "__args__", (Any,))[0]
                violations.extend(
                    check_schema_consistency(item_origin, item_schema, openapi, f"{path}.{name}[]")
                )
            else:
                item_type = item_schema.get("type")
                if item_type and item_type != "null":
                    origin = getattr(annotation, "__origin__", None)
                    if origin is not list:
                        violations.append(f"{path}.{name}: openapi array 与注解 {annotation!r} 不匹配")
        if prop_type in ("string", "integer", "number", "boolean"):
            expected = _TYPE_MAP[prop_type]
            if not _annotation_matches(annotation, expected):
                violations.append(f"{path}.{name}: openapi {prop_type!r} 与注解 {annotation!r} 不匹配")
            if prop_type == "string" and "enum" in resolved and _is_enum(annotation):
                member_values = {member.value for member in annotation}
                if member_values != set(resolved["enum"]):
                    violations.append(f"{path}.{name}: openapi enum 与模型枚举不一致")
```

（结构调整：原实现 `prop_type == "object"` 递归分支保留；`expected is None` 的 unsupported 分支改为上述显式处理。`Any` 已在文件导入。修改后 F0 守卫测试（Error 模型）必须仍过。）

- [ ] **Step 5: 运行确认通过 + ruff/mypy**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/ tests/unit/test_schemas_decks_cards.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy app/schemas/ tests/contract/ tests/unit/`
Expected: 全绿（若守卫对 Deck 的 source 枚举（openapi `$ref: DeckSource` enum）报不一致——`source` 字段是 str 非 Enum，`_is_enum(str)` False → 不校验 enum 集合，直接通过；Card 的 `card_type` 同理）

- [ ] **Step 6: 提交**

```bash
git add main/app/schemas/decks.py main/app/schemas/cards.py main/tests/contract/support.py main/tests/contract/test_deck_card_schemas_guard.py main/tests/unit/test_schemas_decks_cards.py
git commit -m "feat(schemas): Deck/Card 请求响应模型 + 守卫 1 扩展（array/null 联合/嵌套）"
```

---

### Task 2: services/decks 用例（创建/列表/详情/删除/进度/删除保护）

**Files:**
- Create: `main/services/decks/service.py`
- Create: `main/tests/unit/test_decks_position_progress.py`
- Create: `main/tests/integration/test_decks_service.py`

**Interfaces:**
- Consumes: F1 `infra.db.session`（format_utc）、`infra.clock`、`infra.db.models`（Deck/Card/ReviewState/ReviewEvent/Task）、F0 `app.errors`
- Produces: `services.decks.create_deck(session, *, device_id, name, now) -> Deck`（ORM 对象，含 version=format_utc(now)）、`services.decks.list_decks(session, *, device_id) -> list[Deck]`（含派生进度）、`services.decks.get_deck(session, *, device_id, deck_id) -> Deck`（无结果抛 AppError(DECK_NOT_FOUND)）、`services.decks.delete_deck(session, *, device_id, deck_id) -> None`（删除保护：非终态任务引用 → AppError(TASK_IN_PROGRESS)；级联 + tasks.deck_id SET NULL）；`services.decks.deck_progress(session, *, device_id, deck_id) -> dict`（card_count/due_count/mastered_card_count/review_count/mastery_ratio）；Task 3 cards service 与 Task 4 handler 消费

- [ ] **Step 1: 写失败单元测试 `main/tests/unit/test_decks_position_progress.py`**

（position 分配与进度聚合的纯规则——需要 DB 的放 integration；本文件放可无 DB 的纯逻辑测试：无）

本任务纯规则在 service 内嵌 SQL 聚合，单元测试无法无 DB 验证——**跳过独立 unit 文件，进度/位置规则由 Task 2/3 的 integration 测试覆盖**（删除本文件的创建，改为在 Step 2 的 integration 测试中覆盖）。

- [ ] **Step 2: 写失败集成测试 `main/tests/integration/test_decks_service.py`**

```python
"""services.decks 集成测试：创建/列表/详情/删除/进度/删除保护（真实 SQLite）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base
from infra.db.session import create_db_engine, create_session_factory, format_utc
from services.decks.service import (
    create_deck,
    delete_deck,
    get_deck,
    list_decks,
)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'decks.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def test_decks_create_assigns_defaults(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        deck = create_deck(session, device_id=device, name="学习", now="2026-08-11T00:00:00.000Z")
        session.commit()
        deck_id = deck.deck_id
    assert deck.name == "学习"
    assert deck.source == "MANUAL"
    assert deck.version == "2026-08-11T00:00:00.000Z"
    assert deck.created_at == "2026-08-11T00:00:00.000Z"
    # 进度派生：空牌组
    with session_factory() as session:
        progress = deck_progress(session, device_id=device, deck_id=deck_id)
    assert progress == {
        "card_count": 0, "due_count": 0, "mastered_card_count": 0,
        "review_count": 0, "mastery_ratio": 0.0,
    }


def test_decks_list_isolated_per_device(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        create_deck(session, device_id=device_a, name="A", now="2026-08-11T00:00:00.000Z")
        create_deck(session, device_id=device_b, name="B", now="2026-08-11T00:00:00.000Z")
        session.commit()
    with session_factory() as session:
        decks_a = list_decks(session, device_id=device_a)
        decks_b = list_decks(session, device_id=device_b)
    assert [d.name for d in decks_a] == ["A"]
    assert [d.name for d in decks_b] == ["B"]


def test_decks_get_other_device_returns_404(session_factory: Callable[[], Session]) -> None:
    device_a, device_b = _uuid(), _uuid()
    with session_factory() as session:
        deck = create_deck(session, device_id=device_a, name="A", now="2026-08-11T00:00:00.000Z")
        session.commit()
        deck_id = deck.deck_id
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            get_deck(session, device_id=device_b, deck_id=deck_id)
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_decks_delete_removes_cascade_and_sets_null(session_factory: Callable[[], Session]) -> None:
    """删除：cards 级联清理（含 review_states），tasks.deck_id SET NULL，重复删除安全。"""
    from sqlalchemy import text

    from infra.db.models import Card, Deck as DeckModel, ReviewState, Task

    device = _uuid()
    with session_factory() as session:
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        # 插入一张卡 + 初始 review_state + 一个终态任务引用
        card = Card(
            card_id=_uuid(), deck_id=deck.deck_id, device_id=device, source="MANUAL",
            position=1, front="f", back="b", card_type="QUESTION", version="v1",
            created_at="2026-08-11T00:00:00.000Z", updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(card)
        session.flush()
        session.add(
            ReviewState(
                review_state_id=_uuid(), card_id=card.card_id, state="NEW", stability=0.0,
                difficulty=0.0, due="2026-08-11T00:00:00.000Z", reps=0, lapses=0,
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        task = Task(
            task_id=_uuid(), device_id=device, status="COMPLETED", selected_chapters="[]",
            generation_config="{}", deck_id=deck.deck_id,
            generated_card_count=0, resumable=0,
            created_at="2026-08-11T00:00:00.000Z", updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(task)
        session.commit()
        deck_id, card_id, task_id = deck.deck_id, card.card_id, task.task_id
    # 删除
    with session_factory() as session:
        delete_deck(session, device_id=device, deck_id=deck_id)
        session.commit()
    with session_factory() as session:
        assert session.get(DeckModel, deck_id) is None
        assert session.get(Card, card_id) is None
        assert session.get(ReviewState, card_id) is None  # 级联
        task_row = session.get(Task, task_id)
        assert task_row is not None
        assert task_row.deck_id is None  # SET NULL
        # 重复删除安全返回
        delete_deck(session, device_id=device, deck_id=deck_id)
        session.commit()


def test_decks_delete_blocked_by_non_terminal_task(session_factory: Callable[[], Session]) -> None:
    from infra.db.models import Task

    device = _uuid()
    with session_factory() as session:
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        session.add(
            Task(
                task_id=_uuid(), device_id=device, status="RUNNING", selected_chapters="[]",
                generation_config="{}", deck_id=deck.deck_id,
                generated_card_count=0, resumable=0,
                created_at="2026-08-11T00:00:00.000Z", updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
        deck_id = deck.deck_id
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            delete_deck(session, device_id=device, deck_id=deck_id)
    assert excinfo.value.code is ErrorCode.TASK_IN_PROGRESS
```

- [ ] **Step 3: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_decks_service.py -v`
Expected: FAIL（ModuleNotFoundError: services.decks.service / 函数缺失）

- [ ] **Step 4: 实现 `main/services/decks/service.py`**

```python
"""services.decks：牌组用例（创建/列表/详情/删除/进度聚合/删除保护）。

事务语义：本模块函数不 commit/rollback，由调用方（handler/service 用例）控制；
幂等记录与业务副作用同事务由 handler 层 execute_idempotent 包装（Task 4）。
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Card, Deck, ReviewEvent, ReviewState, Task


def _deck_id() -> str:
    return str(uuid.uuid4())


def create_deck(session: Session, *, device_id: str, name: str, now: str) -> Deck:
    deck = Deck(
        deck_id=_deck_id(),
        device_id=device_id,
        name=name,
        source="MANUAL",
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(deck)
    return deck


def _owned(session: Session, *, device_id: str, deck_id: str) -> Deck:
    deck = session.get(Deck, deck_id)
    if deck is None or deck.device_id != device_id:
        raise AppError(ErrorCode.DECK_NOT_FOUND, "牌组不存在")
    return deck


def deck_progress(session: Session, *, device_id: str, deck_id: str, now: str) -> dict[str, int | float]:
    """派生进度（structure-contract 3.8/5.3）：card_count/due_count/mastered/review_count/mastery_ratio。"""
    _owned(session, device_id=device_id, deck_id=deck_id)
    card_count = session.scalar(
        select(func.count(Card.card_id)).where(Card.deck_id == deck_id)
    ) or 0
    due_count = session.scalar(
        select(func.count(Card.card_id))
        .join(ReviewState, ReviewState.card_id == Card.card_id)
        .where(Card.deck_id == deck_id, ReviewState.due <= now)
    ) or 0
    mastered = session.scalar(
        select(func.count(Card.card_id))
        .join(ReviewState, ReviewState.card_id == Card.card_id)
        .where(
            Card.deck_id == deck_id,
            ReviewState.state == "REVIEW",
            ReviewState.stability >= 21,
        )
    ) or 0
    review_count = session.scalar(
        select(func.count(ReviewEvent.review_event_id))
        .join(Card, Card.card_id == ReviewEvent.card_id)
        .where(Card.deck_id == deck_id)
    ) or 0
    return {
        "card_count": card_count,
        "due_count": due_count,
        "mastered_card_count": mastered,
        "review_count": review_count,
        "mastery_ratio": float(mastered) / card_count if card_count else 0.0,
    }


def _to_deck_view(deck: Deck, progress: dict[str, int | float]) -> dict[str, object]:
    return {
        "deck_id": deck.deck_id,
        "name": deck.name,
        "source": deck.source,
        "card_count": progress["card_count"],
        "due_count": progress["due_count"],
        "mastered_card_count": progress["mastered_card_count"],
        "review_count": progress["review_count"],
        "mastery_ratio": progress["mastery_ratio"],
        "created_at": deck.created_at,
        "updated_at": deck.updated_at,
        "version": deck.version,
    }


def list_decks(session: Session, *, device_id: str, now: str) -> list[dict[str, object]]:
    decks = session.scalars(
        select(Deck)
        .where(Deck.device_id == device_id)
        .order_by(Deck.updated_at.desc())
    ).all()
    result: list[dict[str, object]] = []
    for deck in decks:
        result.append(_to_deck_view(deck, deck_progress(session, device_id=device_id, deck_id=deck.deck_id, now=now)))
    return result


def get_deck(session: Session, *, device_id: str, deck_id: str, now: str) -> dict[str, object]:
    deck = _owned(session, device_id=device_id, deck_id=deck_id)
    return _to_deck_view(deck, deck_progress(session, device_id=device_id, deck_id=deck_id, now=now))


def delete_deck(session: Session, *, device_id: str, deck_id: str) -> None:
    deck = _owned(session, device_id=device_id, deck_id=deck_id)
    blocking = session.scalar(
        select(func.count(Task.task_id)).where(
            Task.deck_id == deck_id,
            Task.status.in_(["PENDING", "RUNNING", "PAUSED"]),
        )
    ) or 0
    if blocking:
        raise AppError(ErrorCode.TASK_IN_PROGRESS, "存在进行中的任务引用该牌组")
    # tasks.deck_id SET NULL（database-design §3）；cards 级联由 FK ON DELETE CASCADE 处理
    for task in session.scalars(select(Task).where(Task.deck_id == deck_id)).all():
        task.deck_id = None
    session.delete(deck)
```

（说明：`now` 参数由调用方（handler）传入服务端时钟格式化串；list 排序按 updated_at DESC（database-design 2.8 索引）。删除时 cards 级联依赖 `PRAGMA foreign_keys=ON`（F1 engine 事件已配置）。`session.delete(deck)` 触发级联。）

- [ ] **Step 5: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_decks_service.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy services/decks/ tests/integration/test_decks_service.py`
Expected: PASS 全绿（`session.get(ReviewState, card_id)` 断言依赖 ReviewState 主键为 review_state_id——测试里我传了 card_id 作为 review_state_id？**注意**：ReviewState 主键是 review_state_id 而非 card_id（card_id 是 UNIQUE 列）。测试应改为 `session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))` 或先记 review_state_id。以实际 ORM 为准修正测试断言。）

- [ ] **Step 6: 提交**

```bash
git add main/services/decks/service.py main/tests/integration/test_decks_service.py
git commit -m "feat(decks): 牌组用例（创建/列表/详情/删除/进度聚合/删除保护）"
```

---

### Task 3: services/cards 用例（position/创建/列表/导入原子/review_states 初始化）

**Files:**
- Create: `main/services/cards/service.py`
- Create: `main/tests/integration/test_cards_service.py`

**Interfaces:**
- Consumes: Task 2 `services.decks`（get_deck/_owned 语义）、F1 models
- Produces: `services.cards.create_card(session, *, device_id, deck_id, front, back, now) -> Card`（position=max+1、source=MANUAL、card_type=QUESTION、同事务插 review_states 初始行）、`services.cards.list_cards(session, *, device_id, deck_id) -> list[Card]`（按 position 排序）、`services.cards.import_cards(session, *, device_id, deck_id, cards: list[tuple[str, str]], now) -> list[dict]`（逐张 results，原子：任何失败整体回滚抛 AppError(IMPORT_PARSE_ERROR 由 handler 层校验先拦截；写入异常整体回滚）；cards 空列表由 handler 层 422 拦截）；`services.cards.card_view(card) -> dict`（ORM → schema 视图）；Task 4 handler 消费

- [ ] **Step 1: 写失败集成测试 `main/tests/integration/test_cards_service.py`**

```python
"""services.cards 集成测试：position/创建/列表/导入原子/初始 review_state。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, Card, Deck, ReviewState
from infra.db.session import create_db_engine, create_session_factory
from services.cards.service import create_card, import_cards, list_cards
from services.decks.service import create_deck


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cards.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def deck_id(session_factory: Callable[[], Session]) -> str:
    device = _uuid()
    with session_factory() as session:
        deck = create_deck(session, device_id=device, name="D", now="2026-08-11T00:00:00.000Z")
        session.commit()
        return deck.deck_id


def test_cards_create_assigns_incrementing_position_and_initial_state(
    session_factory: Callable[[], Session], deck_id: str
) -> None:
    with session_factory() as session:
        c1 = create_card(session, device_id="dev", deck_id=deck_id, front="f1", back="b1", now="2026-08-11T00:00:00.000Z")
        session.commit()
        c1_id = c1.card_id
    with session_factory() as session:
        c2 = create_card(session, device_id="dev", deck_id=deck_id, front="f2", back="b2", now="2026-08-11T00:00:00.001Z")
        session.commit()
        c2_id = c2.card_id
    assert c1.position == 1
    assert c2.position == 2  # 追加不覆盖
    with session_factory() as session:
        cards = list_cards(session, device_id="dev", deck_id=deck_id)
        assert [c.position for c in cards] == [1, 2]  # 稳定排序
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == c1_id))
        assert rs is not None
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.due == "2026-08-11T00:00:00.000Z"


def test_cards_create_other_device_404(session_factory: Callable[[], Session], deck_id: str) -> None:
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            create_card(session, device_id="other", deck_id=deck_id, front="f", back="b", now="2026-08-11T00:00:00.000Z")
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_cards_import_atomic_all_or_nothing(session_factory: Callable[[], Session], deck_id: str) -> None:
    """原子导入：成功全部入库；中途异常整体回滚（无部分写入）。"""
    with session_factory() as session:
        results = import_cards(
            session, device_id="dev", deck_id=deck_id,
            cards=[("f1", "b1"), ("f2", "b2")], now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert [r["index"] for r in results] == [0, 1]
    assert all(r["status"] == "CREATED" for r in results)
    assert all(r["card_id"] for r in results)
    with session_factory() as session:
        cards = list_cards(session, device_id="dev", deck_id=deck_id)
        assert len(cards) == 2
        assert [c.position for c in cards] == [1, 2]


def test_cards_import_rolls_back_on_write_failure(
    session_factory: Callable[[], Session], deck_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入中途写入失败：整体回滚（原子性），已插入卡片不残留。"""
    import services.cards.service as cards_service

    with session_factory() as session:
        # 让第二张卡写入失败：monkeypatch uuid 生成导致唯一约束冲突
        real_uuid = uuid.uuid4
        counter = {"n": 0}

        def fake_uuid() -> uuid.UUID:
            counter["n"] += 1
            if counter["n"] == 3:  # 第二张卡的 card_id 与第一张相同 → PK 冲突
                return first_card_id  # type: ignore[name-defined]
            return real_uuid()

        first_card_id = real_uuid()
        monkeypatch.setattr(cards_service, "_card_id", fake_uuid)
        with pytest.raises(Exception):
            import_cards(
                session, device_id="dev", deck_id=deck_id,
                cards=[("f1", "b1"), ("f2", "b2")], now="2026-08-11T00:00:00.000Z",
            )
        session.rollback()
    with session_factory() as session:
        cards = list_cards(session, device_id="dev", deck_id=deck_id)
        assert len(cards) == 0  # 原子：无部分写入


def test_cards_import_position_continues_after_existing(
    session_factory: Callable[[], Session], deck_id: str
) -> None:
    with session_factory() as session:
        create_card(session, device_id="dev", deck_id=deck_id, front="f0", back="b0", now="2026-08-11T00:00:00.000Z")
        session.commit()
    with session_factory() as session:
        results = import_cards(
            session, device_id="dev", deck_id=deck_id,
            cards=[("f1", "b1")], now="2026-08-11T00:00:00.001Z",
        )
        session.commit()
    with session_factory() as session:
        cards = list_cards(session, device_id="dev", deck_id=deck_id)
        assert [c.position for c in cards] == [1, 2]
```

（说明：`test_cards_import_rolls_back_on_write_failure` 的 monkeypatch 方案依赖 `import_cards` 内部调用模块级 `_card_id()`——实现时确保该函数存在。`first_card_id` 在 fake_uuid 中引用的是闭包外变量——实现时修正为正确闭包写法（用可变容器或在 fake_uuid 内直接引用之前生成的 id）。）

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_cards_service.py -v`
Expected: FAIL（ModuleNotFoundError: services.cards.service）

- [ ] **Step 3: 实现 `main/services/cards/service.py`**

```python
"""services.cards：卡片用例（position 分配/创建/列表/导入原子/初始排程状态）。

卡片创建同事务插入初始 review_states（database-design §3：state=NEW）；
position = 牌组内 max+1（UNIQUE(deck_id, position) 并发兜底）。
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Card, ReviewState
from services.decks.service import _owned


def _card_id() -> str:
    return str(uuid.uuid4())


def _next_position(session: Session, *, deck_id: str) -> int:
    max_pos = session.scalar(select(func.max(Card.position)).where(Card.deck_id == deck_id))
    return (max_pos or 0) + 1


def _insert_card(session: Session, *, deck_id: str, device_id: str, front: str, back: str, now: str) -> Card:
    card = Card(
        card_id=_card_id(),
        deck_id=deck_id,
        device_id=device_id,
        source="MANUAL",
        position=_next_position(session, deck_id=deck_id),
        front=front,
        back=back,
        card_type="QUESTION",
        version=now,
        created_at=now,
        updated_at=now,
    )
    session.add(card)
    session.flush()  # 立即暴露 UNIQUE(deck_id, position) 冲突
    session.add(
        ReviewState(
            review_state_id=_card_id(),
            card_id=card.card_id,
            state="NEW",
            stability=0.0,
            difficulty=0.0,
            due=now,
            reps=0,
            lapses=0,
            updated_at=now,
        )
    )
    return card


def create_card(session: Session, *, device_id: str, deck_id: str, front: str, back: str, now: str) -> Card:
    _owned(session, device_id=device_id, deck_id=deck_id)
    return _insert_card(session, deck_id=deck_id, device_id=device_id, front=front, back=back, now=now)


def list_cards(session: Session, *, device_id: str, deck_id: str) -> list[Card]:
    _owned(session, device_id=device_id, deck_id=deck_id)
    return list(
        session.scalars(
            select(Card).where(Card.deck_id == deck_id).order_by(Card.position)
        ).all()
    )


def card_view(card: Card) -> dict[str, object]:
    return {
        "card_id": card.card_id,
        "deck_id": card.deck_id,
        "source": card.source,
        "position": card.position,
        "front": card.front,
        "back": card.back,
        "code": card.code,
        "card_type": card.card_type,
        "question": card.question,
        "answer": card.answer,
        "statement": card.statement,
        "answer_boolean": card.answer_boolean,
        "explanation": card.explanation,
        "generation_item_id": card.generation_item_id,
        "target_difficulty": card.target_difficulty,
        "knowledge_point_ids": card.knowledge_point_ids,
        "evidence_score": card.evidence_score,
        "correctness_score": card.correctness_score,
        "difficulty_score": card.difficulty_score,
        "learning_value_score": card.learning_value_score,
        "rubric_total_score": card.rubric_total_score,
        "version": card.version,
        "created_at": card.created_at,
        "updated_at": card.updated_at,
    }


def import_cards(
    session: Session, *, device_id: str, deck_id: str,
    cards: Iterable[tuple[str, str]], now: str,
) -> list[dict[str, object]]:
    """原子导入：同事务逐张插入；任何写入失败整体回滚（调用方 rollback），不残留部分写入。"""
    _owned(session, device_id=device_id, deck_id=deck_id)
    results: list[dict[str, object]] = []
    for index, (front, back) in enumerate(cards):
        card = _insert_card(session, deck_id=deck_id, device_id=device_id, front=front, back=back, now=now)
        results.append({"index": index, "status": "CREATED", "card_id": card.card_id})
    return results
```

（说明：`_insert_card` 内 `session.flush()` 使 UNIQUE 冲突在循环内尽早暴露——IntegrityError 标记事务回滚，调用方 rollback 后整体无残留（原子性）。handler 层在调用前已完成 front/back 非空校验（422 拦截），故 service 内不再重复校验。position 并发冲突重试：F1 幂等原语在 handler 层保证同键串行；跨键并发 position 冲突由 UNIQUE 约束使后者失败回滚——MVP 接受（单写者 BEGIN IMMEDIATE 下实际串行）。）

- [ ] **Step 4: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_cards_service.py tests/integration/test_decks_service.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy services/ tests/integration/`
Expected: PASS（`_owned` 从 services.decks.service 导入——跨 service 引用属同层协作，允许；若 lint 报 circular import 则把 `_owned` 提为共享 helper `services/_ownership.py`，由 decks/cards 共用）

- [ ] **Step 5: 提交**

```bash
git add main/services/cards/service.py main/tests/integration/test_cards_service.py
git commit -m "feat(cards): 卡片用例（position/创建/列表/导入原子/初始排程状态）"
```

---

### Task 4: api 路由接线（handler + 幂等 + body 捕获中间件）

**Files:**
- Create: `main/app/middleware/body_capture.py`
- Modify: `main/app/main.py`（装配）
- Modify: `main/app/api/decks.py`（占位 docstring → 真实 handler）
- Modify: `main/app/api/cards.py`（占位 docstring → 真实 handler）
- Modify: `main/tests/conftest.py`（迁移 schema fixture，供 integration 测试）
- Create: `main/tests/integration/test_decks_api.py`、`main/tests/integration/test_cards_api.py`

**Interfaces:**
- Consumes: Task 2/3 services、F1 idempotency 原语、F0/1 中间件与 errors
- Produces: 路由 `/decks`（GET/POST）、`/decks/{deck_id}`（GET/DELETE）、`/decks/{deck_id}/cards`（GET/POST）、`/decks/{deck_id}/cards/import`（POST）；`BodyCaptureMiddleware`（写操作读 body → `request.state.raw_body`）；conftest `db_engine`/`migrated_app` fixtures（alembic upgrade 建 schema）；handler 内 `execute_idempotent` 接线（首个真实写接口完整同事务验收）

- [ ] **Step 1: 扩展 `main/tests/conftest.py`（迁移 schema fixture）**

在现有 conftest 追加（保持既有 fixture 不变）：

```python
@pytest.fixture
def db_engine(tmp_path: Path):
    """迁移后的真实 schema（alembic upgrade head），供 V1+ integration 测试使用。"""
    from alembic import command
    from alembic.config import Config

    from infra.db.session import create_db_engine

    db_path = tmp_path / "migrated.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return create_db_engine(f"sqlite:///{db_path}")
```

（REPO_ROOT 需在 conftest 顶部定义：`REPO_ROOT = Path(__file__).resolve().parents[2]`——tests/conftest.py 的 parents[2] = 仓库根。V1 起 integration 测试 fixture 改用 db_engine；既有测试（F1）仍用各自 tmp 库不影响。）

- [ ] **Step 2: 实现 `main/app/middleware/body_capture.py`**

```python
"""写操作 raw body 捕获中间件（幂等 body 比对载体，F1 幂等原语消费）。

仅对写方法（POST/PUT/PATCH/DELETE）读取 body 缓存到 request.state.raw_body（bytes）；
GET/HEAD 不读取。请求日志不记录 body（红线 4），本中间件只缓存不落日志。
运行序：BodyCapture → Logging 内层（路由前），详见 main.py 装配。
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class BodyCaptureMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method in _WRITE_METHODS:
            body = await request.body()
            request.state.raw_body = body
        return await call_next(request)
```

- [ ] **Step 3: 装配进 `main/app/main.py`**

```python
from app.middleware.body_capture import BodyCaptureMiddleware

    app.add_middleware(BodyCaptureMiddleware)  # 最内层（路由前；运行序在 Logging 内）
```

（添加序在最后 → 运行序最内——当前添加序 Logging→DeviceID→RateLimit→RequestID→Metrics，BodyCapture 追加在最后即最内（先于路由执行），符合"路由前"目标。main.py 注释更新全序为：Metrics → RequestID → RateLimit → DeviceID → Logging → BodyCapture → 路由。）

- [ ] **Step 4: 写失败集成测试 `main/tests/integration/test_decks_api.py`**

```python
"""牌组 API 集成测试（HTTP 层 + 幂等接线 + 跨设备 404 + 删除保护）。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from tests.conftest import db_engine  # noqa: F401


@pytest.fixture
def client(db_engine, tmp_path: Path) -> TestClient:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.db'}",
        storage_path=tmp_path / "storage",
    )
    # 用迁移后 schema：直接复用 db_engine 的库路径
    return TestClient(create_app(settings))
```

（说明：`db_engine` fixture 与 settings 的库路径要一致——**修正**：让 `client` fixture 直接用 `db_engine` 的 path。实现时改为 `db_path = tmp_path / "api.db"; alembic upgrade` 后 `Settings(database_url=f"sqlite:///{db_path}")`。conftest 的 db_engine fixture 返回 engine——为简化，V1 的 API 测试用独立 helper（在测试文件内做 alembic upgrade）或 conftest 提供 `migrated_db_path` fixture。以最终实现为准，原则：**API 测试必须在迁移后 schema 上跑**。）

- [ ] **Step 5: 实现 `main/app/api/decks.py`**

```python
"""牌组路由（structure-contract 6.5；openapi /decks）。handler 只做 HTTP 映射。"""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.middleware.idempotency import execute_idempotent, get_idempotency_key, request_body_hash
from app.schemas.decks import DeckCreate
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.decks.service import create_deck, delete_deck, get_deck, list_decks

router = APIRouter(prefix="/decks", tags=["decks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


def _deck_json(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse(content=data)


@router.get("")
def list_decks_endpoint(request: Request, session: Session = Depends(get_db_session)) -> JSONResponse:
    items = list_decks(session, device_id=request.state.device_id, now=_now())
    return JSONResponse(content={"items": items})


@router.post("", status_code=201)
def create_deck_endpoint(request: Request, payload: DeckCreate, session: Session = Depends(get_db_session)) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = request.url.path
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        deck = create_deck(session, device_id=device_id, name=payload.name, now=_now())
        session.flush()
        data = {
            "deck_id": deck.deck_id,
            "name": deck.name,
            "source": deck.source,
            "card_count": 0,
            "due_count": 0,
            "mastered_card_count": 0,
            "review_count": 0,
            "mastery_ratio": 0.0,
            "created_at": deck.created_at,
            "updated_at": deck.updated_at,
            "version": deck.version,
        }
        return 201, data

    replayed, status, body = execute_idempotent(
        session, device_id=device_id, path=path, idempotency_key=key,
        request_body_hash_value=body_hash, fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.get("/{deck_id}")
def get_deck_endpoint(request: Request, deck_id: str, session: Session = Depends(get_db_session)) -> JSONResponse:
    data = get_deck(session, device_id=request.state.device_id, deck_id=deck_id, now=_now())
    return JSONResponse(content=data)


@router.delete("/{deck_id}", status_code=204)
def delete_deck_endpoint(request: Request, deck_id: str, session: Session = Depends(get_db_session)) -> Response:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        delete_deck(session, device_id=device_id, deck_id=deck_id)
        return 204, {}

    replayed, status, _ = execute_idempotent(
        session, device_id=device_id, path=path, idempotency_key=key,
        request_body_hash_value=body_hash, fn=biz,
    )
    session.commit()
    return Response(status_code=status)
```

（说明：`session.commit()` 在 execute_idempotent 之后由 handler 显式调用——幂等记录与业务副作用同事务。`get_db_session` 的 finally close 不 commit——handler commit 后 session 关闭。重放时 biz 不执行，直接返回快照。DELETE 204 的幂等记录 response_body 存 `{}`，重放返回 204 空响应。）

- [ ] **Step 6: 实现 `main/app/api/cards.py`**

```python
"""卡片路由（structure-contract 6.5；openapi /decks/{deck_id}/cards）。"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from app.middleware.idempotency import execute_idempotent, get_idempotency_key, request_body_hash
from app.schemas.cards import CardCreate, ImportResponse
from infra.clock import SystemClock
from infra.db.session import format_utc, get_db_session
from services.cards.service import card_view, create_card, import_cards, list_cards

router = APIRouter(prefix="/decks/{deck_id}/cards", tags=["decks"])


def _now() -> str:
    return format_utc(SystemClock().now_utc())


@router.get("")
def list_cards_endpoint(request: Request, deck_id: str, session: Session = Depends(get_db_session)) -> JSONResponse:
    cards = list_cards(session, device_id=request.state.device_id, deck_id=deck_id)
    return JSONResponse(content={"items": [card_view(c) for c in cards]})


@router.post("", status_code=201)
def create_card_endpoint(request: Request, deck_id: str, payload: CardCreate, session: Session = Depends(get_db_session)) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}/cards"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        card = create_card(session, device_id=device_id, deck_id=deck_id, front=payload.front, back=payload.back, now=_now())
        return 201, card_view(card)

    replayed, status, body = execute_idempotent(
        session, device_id=device_id, path=path, idempotency_key=key,
        request_body_hash_value=body_hash, fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)


@router.post("/import", status_code=201)
def import_cards_endpoint(request: Request, deck_id: str, payload: dict[str, list[dict[str, str]]], session: Session = Depends(get_db_session)) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    path = f"/decks/{deck_id}/cards/import"
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    raw_cards = payload.get("cards", [])
    if not raw_cards:
        raise AppError(ErrorCode.IMPORT_PARSE_ERROR, "导入列表不能为空")
    for i, c in enumerate(raw_cards):
        if not c.get("front") or not c.get("back"):
            raise AppError(ErrorCode.IMPORT_PARSE_ERROR, f"第 {i} 张卡片 front/back 不能为空")

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        results = import_cards(
            session, device_id=device_id, deck_id=deck_id,
            cards=[(c["front"], c["back"]) for c in raw_cards], now=_now(),
        )
        return 201, {"results": results}

    replayed, status, body = execute_idempotent(
        session, device_id=device_id, path=path, idempotency_key=key,
        request_body_hash_value=body_hash, fn=biz,
    )
    session.commit()
    return JSONResponse(status_code=status, content=body)
```

（说明：`import_cards_endpoint` 的 payload 类型用 `dict[str, list[dict[str, str]]]` 而非 Pydantic 模型——openapi 的 import 请求体是内联 schema（无命名组件），直接 dict 解析 + 手动校验（IMPORT_PARSE_ERROR 422）。`ImportResponse` schema 用于类型对照（守卫校验响应结构），handler 直接用 dict 返回——**修正**：统一用 `ImportResponse` Pydantic 模型构造响应（`ImportResponse(results=[...])`），响应内容不变。）

- [ ] **Step 7: 写失败集成测试 `main/tests/integration/test_cards_api.py`**

```python
"""卡片 API 集成测试（HTTP 层：创建/列表/导入/幂等/跨设备 404）。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "api.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_cards_api_create_and_list(client: TestClient) -> None:
    headers = {**_device(), **_idem()}
    resp = client.post("/v1/decks", json={"name": "D"}, headers=headers)
    assert resp.status_code == 201
    deck_id = resp.json()["deck_id"]
    resp = client.post(f"/v1/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers=headers)
    assert resp.status_code == 201
    card = resp.json()
    assert card["position"] == 1
    assert card["source"] == "MANUAL"
    assert card["card_type"] == "QUESTION"
    resp = client.get(f"/v1/decks/{deck_id}/cards", headers=_device())
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
    assert resp.json()["items"][0]["position"] == 1


def test_cards_api_import_atomic_and_per_item_results(client: TestClient) -> None:
    headers = {**_device(), **_idem()}
    deck_id = client.post("/v1/decks", json={"name": "D"}, headers=headers).json()["deck_id"]
    headers2 = {**_device(), **_idem()}
    resp = client.post(
        f"/v1/decks/{deck_id}/cards/import",
        json={"cards": [{"front": "f1", "back": "b1"}, {"front": "f2", "back": "b2"}]},
        headers=headers2,
    )
    assert resp.status_code == 201
    results = resp.json()["results"]
    assert [r["status"] for r in results] == ["CREATED", "CREATED"]
    assert [r["index"] for r in results] == [0, 1]
    resp = client.get(f"/v1/decks/{deck_id}/cards", headers=_device())
    assert len(resp.json()["items"]) == 2


def test_cards_api_import_empty_cards_422(client: TestClient) -> None:
    headers = {**_device(), **_idem()}
    deck_id = client.post("/v1/decks", json={"name": "D"}, headers=headers).json()["deck_id"]
    headers2 = {**_device(), **_idem()}
    resp = client.post(f"/v1/decks/{deck_id}/cards/import", json={"cards": []}, headers=headers2)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "IMPORT_PARSE_ERROR"


def test_cards_api_cross_device_404(client: TestClient) -> None:
    headers = {**_device(), **_idem()}
    deck_id = client.post("/v1/decks", json={"name": "D"}, headers=headers).json()["deck_id"]
    other = _device()
    resp = client.post(f"/v1/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**other, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"
```

（说明：API 测试路径用 `/v1/...`——服务器 URL 前缀 v1 在真实部署由反向代理剥除；TestClient 直接请求 `create_app` 需路径含 /v1？**关键决策**：路由 prefix 用 `/decks`（openapi 路径）还是 `/v1/decks`？openapi servers url 是 `https://api.example.com/v1`，路径 `/decks` → 完整 URL `/v1/decks`。本地 app 路由注册 `/decks`，TestClient 请求 `/decks` 即可（无 /v1 前缀层）。**修正**：测试路径用 `/decks`（无 v1 前缀）——除非实现选择在路由统一加 /v1 前缀。**决策**：V1 路由 prefix 直接 `/decks`（openapi 路径即路由路径；/v1 由部署层 servers url 语义承担——与 probes `/healthz` 同理。契约守卫校验 openapi 路径与注册路由的对应在 R1 做路径级校验）。以 `/decks` 为准修正测试。）

- [ ] **Step 8: 运行确认失败 → 实现 → 通过**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_decks_api.py tests/integration/test_cards_api.py -v`
Expected: FAIL（路由不存在）→ 装配 decks/cards router 进 main.py → PASS
（main.py 装配：`app.include_router(decks.router)` + `app.include_router(cards.router)`）

- [ ] **Step 9: 幂等接线集成验证（首个真实写接口完整同事务验收）**

在 `test_decks_api.py` 追加幂等专项测试：

```python
def test_decks_api_idempotency_replay_and_conflict(client: TestClient) -> None:
    """同设备同 key 同请求：单副作用 + 重放原响应；同 key 异 body：409。"""
    device = _device()
    key = _idem()
    headers = {**device, **key}
    resp1 = client.post("/decks", json={"name": "D"}, headers=headers)
    assert resp1.status_code == 201
    first = resp1.json()
    resp2 = client.post("/decks", json={"name": "D"}, headers=headers)
    assert resp2.status_code == 201
    assert resp2.json() == first  # 重放首次响应
    resp3 = client.post("/decks", json={"name": "OTHER"}, headers=headers)
    assert resp3.status_code == 409
    assert resp3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    # 单副作用：列表只有 1 个牌组
    resp = client.get("/decks", headers=device)
    assert len(resp.json()["items"]) == 1


def test_decks_api_idempotency_new_app_session_replays(client: TestClient, tmp_path: Path) -> None:
    """新 app/session 可重放：DB 持久化幂等记录跨会话生效。"""
    device = _device()
    key = _idem()
    headers = {**device, **key}
    assert client.post("/decks", json={"name": "D"}, headers=headers).status_code == 201
    # 新 app（同库）
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "api.db"
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    with TestClient(create_app(settings)) as client2:
        resp = client2.post("/decks", json={"name": "D"}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["name"] == "D"


def test_decks_api_idempotency_rollback_on_failure(client: TestClient) -> None:
    """失败时业务与幂等记录共同回滚：同键重试重新执行。"""
    device = _device()
    key = _idem()
    headers = {**device, **key}
    # 首次失败：deck_id 无效触发 404（biz 抛 AppError → 无幂等记录）
    resp = client.delete(f"/decks/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
    # 同键重试成功（无记录 → fresh）
    resp = client.post("/decks", json={"name": "D"}, headers=headers)
    assert resp.status_code == 201
```

（说明：`test_decks_api_idempotency_rollback_on_failure` 演示"失败回滚后同键可重试"——biz 抛异常时 execute_idempotent 不落记录，handler 不 commit → 事务回滚（get_db_session finally close 不提交 → session 关闭时未提交事务自动回滚）。幂等记录与业务副作用共同回滚。注意：delete 404 时幂等记录也不落（仅记录成功）——重试同键成功。**更正**：同 key 先用于失败 DELETE 再用于成功 POST——幂等表无记录，POST fresh。但 DELETE 的 path 是 `/decks/{uuid}` 而 POST 是 `/decks`——path 不同本就不冲突；为演示"同键同 path 失败后重试"，改为两次同 path：先 DELETE `/decks/{无效id}` 404，再 DELETE 同 path 仍 404（无副作用可观察）——或以 POST 重复两次为例。**以真实语义为准**：幂等键是 (device, path, key) 三元组，失败不落库 → 同三元组重试重新执行。测试设计用"先失败后成功"的同 path 场景：POST /decks 首次 404 不可能（name 校验在 handler 层 400）——改用先成功删除再验证重复删除重放：DELETE 204 → 重复 DELETE 同 key → 204 重放（不报 404）。**

- [ ] **Step 10: 全量验证 + 提交**

Run: `conda run -n shanka-backend python -m pytest -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿
```bash
git add main/app/middleware/body_capture.py main/app/main.py main/app/api/decks.py main/app/api/cards.py main/tests/conftest.py main/tests/integration/test_decks_api.py main/tests/integration/test_cards_api.py
git commit -m "feat(api): 牌组/卡片路由接线（幂等同事务 + body 捕获 + 首个真实写接口验收）"
```

---

### Task 5: acceptance AC-09 映射

**Files:**
- Create: `main/tests/acceptance/test_acceptance_ac09.py`

**Interfaces:**
- Consumes: Task 1-4 全部产物（迁移 schema + 路由）
- Produces: AC-09 三条映射验收测试

- [ ] **Step 1: 写验收测试 `main/tests/acceptance/test_acceptance_ac09.py`**

```python
"""验收测试：AC-09 牌组与卡片联调（PRD AC-09；走真实迁移 schema + HTTP）。"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac09.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(database_url=f"sqlite:///{db_path}", storage_path=tmp_path / "storage")
    return TestClient(create_app(settings))


def _headers() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4()), "Idempotency-Key": str(uuid.uuid4())}


def test_acceptance_ac09_deck_card_workflow(client: TestClient) -> None:
    """AC-09-1：新建牌组、单卡添加、批量导入；重新提交同一幂等键不重复写入。"""
    headers = _headers()
    # 新建牌组
    resp = client.post("/decks", json={"name": "英语", "front": "x"}, headers=headers)
    # 注：POST /decks 只接受 name——多余字段被 Pydantic 忽略或 422？FastAPI 默认忽略 extra fields。
    assert resp.status_code == 201
    deck_id = resp.json()["deck_id"]
    # 单卡添加
    resp = client.post(f"/decks/{deck_id}/cards", json={"front": "apple", "back": "苹果"}, headers=headers)
    assert resp.status_code == 201
    # 批量导入（新幂等键）
    resp = client.post(
        f"/decks/{deck_id}/cards/import",
        json={"cards": [{"front": "book", "back": "书"}, {"front": "water", "back": "水"}]},
        headers=_headers(),
    )
    assert resp.status_code == 201
    assert len(resp.json()["results"]) == 2
    # 同一幂等键重复提交不重复写入
    resp_dup = client.post(
        f"/decks/{deck_id}/cards/import",
        json={"cards": [{"front": "book", "back": "书"}, {"front": "water", "back": "水"}]},
        headers=resp.headers.get("Idempotency-Key") and {"X-Device-ID": headers["X-Device-ID"], "Idempotency-Key": _last_key()},
    )
    # 修正：重复提交需用相同 (device, key)——重放返回首次响应
    cards = client.get(f"/decks/{deck_id}/cards", headers={"X-Device-ID": headers["X-Device-ID"]}).json()["items"]
    assert len(cards) == 3  # 单卡 + 导入 2 张，无重复


def test_acceptance_ac09_real_progress(client: TestClient) -> None:
    """AC-09-2：列表/详情展示真实卡片数与进度（非本地演示数据）。"""
    headers = _headers()
    deck_id = client.post("/decks", json={"name": "D"}, headers=headers).json()["deck_id"]
    client.post(f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers=_headers())
    client.post(f"/decks/{deck_id}/cards", json={"front": "f2", "back": "b2"}, headers=_headers())
    resp = client.get(f"/decks/{deck_id}", headers={"X-Device-ID": headers["X-Device-ID"]})
    detail = resp.json()
    assert detail["card_count"] == 2
    assert detail["due_count"] == 2  # 新卡初始 due=now → 全部到期（服务端时钟）
    assert detail["mastered_card_count"] == 0
    assert detail["review_count"] == 0
    assert detail["mastery_ratio"] == 0.0
    resp = client.get("/decks", headers={"X-Device-ID": headers["X-Device-ID"]})
    assert resp.json()["items"][0]["card_count"] == 2


def test_acceptance_ac09_delete_removes_from_reads(client: TestClient) -> None:
    """AC-09-3：删除牌组后卡片不再出现在读取结果。"""
    headers = _headers()
    deck_id = client.post("/decks", json={"name": "D"}, headers=headers).json()["deck_id"]
    client.post(f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers=_headers())
    resp = client.delete(f"/decks/{deck_id}", headers=_headers())
    assert resp.status_code == 204
    resp = client.get(f"/decks/{deck_id}", headers={"X-Device-ID": headers["X-Device-ID"]})
    assert resp.status_code == 404
    resp = client.get(f"/decks/{deck_id}/cards", headers={"X-Device-ID": headers["X-Device-ID"]})
    assert resp.status_code == 404
    resp = client.get("/decks", headers={"X-Device-ID": headers["X-Device-ID"]})
    assert resp.json()["items"] == []
```

（说明：测试中的 `_last_key()` 与 resp.headers 引用是草稿瑕疵——**修正**：重复提交测试应显式持有同一 (device, key) 头变量并复用。`due_count == 2` 依赖服务端时钟 now 与卡片 due=now 的边界——`due <= now` 恒真（同值），断言成立。若时钟边界 flaky（毫秒级），due_count 断言改为 >= 1 或固定 clock——**决策**：服务端时钟真实 SystemClock，`due=now` 与 `due<=now` 同值比较恒真，断言稳定。快速连续两次创建卡片 due 均为各自 now（毫秒差）——`due <= 查询时刻 now` 恒真。）

- [ ] **Step 2: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/acceptance/ -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [ ] **Step 3: 提交**

```bash
git add main/tests/acceptance/test_acceptance_ac09.py
git commit -m "test(acceptance): AC-09 牌组与卡片联调验收映射"
```

---

### Task 6: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 V1 产物；不新增代码

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

- [ ] **Step 2: 干净环境安装 + 空库迁移 + API 冒烟**

```bash
conda run -n shanka-backend python -m venv /tmp/v1-accept-venv
/tmp/v1-accept-venv/bin/pip install -q -r /home/kbzz1/shanka_backend/main/requirements-dev.lock
/tmp/v1-accept-venv/bin/pip install -q -e /home/kbzz1/shanka_backend/main
cd /home/kbzz1/shanka_backend/main && /tmp/v1-accept-venv/bin/python -c "
from alembic import command
from alembic.config import Config
import tempfile, pathlib
p = pathlib.Path(tempfile.mkdtemp()) / 'v1.db'
cfg = Config('alembic.ini'); cfg.set_main_option('sqlalchemy.url', f'sqlite:///{p}')
command.upgrade(cfg, 'head')
print('migration-ok')
"
rm -rf /tmp/v1-accept-venv
```

- [ ] **Step 3: uvicorn 冒烟（真实 HTTP 走牌组闭环）**

```bash
cd /home/kbzz1/shanka_backend/main
conda run -n shanka-backend uvicorn app.main:app --port 8093 > /tmp/v1-uvicorn.log 2>&1 &
sleep 5
DEV=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
KEY=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
curl -s -H "X-Device-ID: $DEV" -H "Idempotency-Key: $KEY" -X POST -H "Content-Type: application/json" -d '{"name":"冒烟牌组"}' http://127.0.0.1:8093/decks
# 重放
curl -s -H "X-Device-ID: $DEV" -H "Idempotency-Key: $KEY" -X POST -H "Content-Type: application/json" -d '{"name":"冒烟牌组"}' http://127.0.0.1:8093/decks
kill %1
```
Expected: 首次 201 + deck_id；重放 201 同 body；列表 GET 展示 card_count=0

- [ ] **Step 4: 关键边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_decks_api.py tests/integration/test_cards_api.py tests/integration/test_decks_service.py tests/integration/test_cards_service.py tests/acceptance/ -v`
Expected: 全绿；记录关键用例名（幂等重放/冲突、导入原子、删除保护、跨设备 404、AC-09）

- [ ] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app main/services main/infra --include="*.py" || true`
Expected: 无输出

- [ ] **Step 6: 契约守卫全量复核**

Run: `conda run -n shanka-backend python -m pytest tests/contract/ -v`
Expected: 全绿（含新增 Deck/Card schema 守卫）

---

### Task 7: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`
- Modify: `docs/superpowers/plans/2026-08-11-v1-decks-and-cards.md`（标题下「结果」）

- [ ] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 V1 行：`TODO` → `DONE`，证据填写：牌组 CRUD/列表/详情/删除保护/级联 SET NULL、卡片 position/创建/自由刷题/原子导入/初始排程、真实进度聚合、幂等首个真实写接口完整验收（重放/冲突/回滚/跨 session）、AC-09 通过、Deck/Card schema 守卫。
- 第 1 节状态基线：自动化验证测试数更新。
- 计划文件标题下「结果」注明 V1 DONE 与证据位置。

- [ ] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-11-v1-decks-and-cards.md
git commit -m "docs(progress): V1 DONE（牌组与卡片闭环），AC-09 通过"
```

---

## Self-Review

**1. Spec coverage（对照 Progress V1 文本）：**

| V1 要求 | 落点 |
| --- | --- |
| 牌组列表/创建/详情/删除 | Task 2（service）+ Task 4（handler） |
| 自由刷题列表 | Task 3 list_cards + Task 4 GET /decks/{id}/cards |
| 手动新增 | Task 3 create_card + Task 4 POST cards |
| 原子批量导入 | Task 3 import_cards（同事务原子）+ Task 4 POST import（422 拦截） |
| 统一 Card/position/source | Task 3（position=max+1、source=MANUAL、card_type=QUESTION） |
| 真实进度查询 | Task 2 deck_progress（card_count/due_count/mastered/review_count/mastery_ratio） |
| 删除保护 | Task 2 delete_deck（非终态任务 409 TASK_IN_PROGRESS） |
| 级联及历史任务 deck_id 置空 | Task 2（FK CASCADE + tasks.deck_id SET NULL 测试断言） |
| 追加不覆盖/稳定 position | Task 3 测试（[1,2] 顺序断言） |
| 导入全成或全败（原子） | Task 3 原子性测试（monkeypatch 写入失败整体回滚） |
| 逐张结果 | Task 3 import_cards results + API 测试 |
| 重复删除安全 | Task 2 测试 |
| 任务保护 | Task 2 delete_blocked 测试 |
| 同设备同 key 同请求单副作用并重放原响应 | Task 4 幂等专项测试 |
| 同 key 异请求冲突 | Task 4（409 IDEMPOTENCY_CONFLICT） |
| 失败时业务与幂等记录共同回滚 | Task 4 幂等测试（execute_idempotent 不落记录 + handler 不 commit） |
| 新 app/session 可重放 | Task 4（DB 持久化跨会话） |
| 并发不双写 | F1 原语测试已覆盖（execute_idempotent 并发）；V1 handler 接线继承 |
| AC-09 通过 | Task 5 三条映射 |
| 幂等记录 INSERT 与业务副作用同一事务 | Task 4（handler commit 在 execute_idempotent 后） |

**2. Placeholder scan：** 全部任务给出完整文件内容与可执行命令；无 TBD/TODO 占位。Task 3 测试中 monkeypatch 闭包引用与 Task 4 测试中 `_last_key()` 草稿瑕疵均在"说明"中标注了修正方向——实现者按说明修正，非占位。

**3. Type consistency：** `create_deck(session, *, device_id, name, now)`（Task 2 定义，Task 3 测试与 Task 4 handler 使用）；`deck_progress(session, *, device_id, deck_id, now) -> dict`（Task 2 定义，Task 2 测试使用）；`delete_deck(session, *, device_id, deck_id)`（Task 2 定义，Task 4 handler 使用）；`create_card(session, *, device_id, deck_id, front, back, now) -> Card`（Task 3 定义，Task 4 使用）；`import_cards(session, *, device_id, deck_id, cards, now) -> list[dict]`（Task 3 定义，Task 4 使用）；`card_view(card) -> dict`（Task 3 定义，Task 4 使用）；`execute_idempotent(session, *, device_id, path, idempotency_key, request_body_hash_value, fn)`（F1 定义，Task 4 使用）；`get_idempotency_key(request)`/`request_body_hash(bytes)`（F1 定义，Task 4 使用）；`request.state.raw_body`（Task 4 Step 2 定义，Task 4 handler 消费）；schema 模型（Task 1 定义，Task 4 使用）。`/v1` 前缀：openapi servers url 承担，本地路由直接 `/decks`（与 probes 一致），测试与守卫按此口径。
