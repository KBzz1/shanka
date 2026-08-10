# V5A 分批生成与质量观测闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**结果**：V5A DONE（2026-08-11）。验收与证据见 docs/Progress.md 第 4 节 V5A 行（10 commits 9bc4301..ae3f498 + fix 7c223a5 + 契约同步 89c3aa8，分支 codex/v5a）：313 用例全绿、四工具通过、干净安装+迁移、uvicorn 冒烟（空态聚合/metrics）、边界 61 用例全绿、AC-04/07 通过、R-16 RESOLVED。全任务 checklist 勾选完成。

**Goal:** 实现按知识点分批生成（每批最多 2 次重试共 3 次尝试）、JSON Schema 唯一入库门槛（card.schema.json）、Rubric 只观测（4 维度 0-3 分，fake judge——LOCAL-DONE 不触网）、批次/知识点状态与游标/计数原子推进（失败批次 SKIPPED 后继续）、usage/版本/质量观测（provider 原样字段与内部统一字段映射、Prompt Cache、cost 估算按生效日期价格常量、metrics 指标）、批次列表与质量聚合 API，使 V5A 依据真实验收证据标记 DONE 且 AC-04/07 通过。

**Architecture:** 契约驱动分层。V5A 建立在 V3B/V4 地基上：`infra/llm/deepseek.py`（adapter + mock transport）、`infra/llm/prompts.py`（Prompt 组装）、`services/tasks/service.py`（任务状态机）、`services/generation/planning.py`（知识点）。新增：`services/generation/schema_validator.py`（card.schema.json 加载 + jsonschema 校验）、`services/generation/batches.py`（分批执行核心：批次状态机/重试/游标/原子推进 + adapter 驱动生成）、`services/generation/rubric.py`（deterministic fake judge + 分数落库）、`services/generation/cost.py`（价格配置常量 + 成本估算）、`app/api/tasks.py` 扩展（GET /tasks/{id}/batches）+ `app/api/observability.py`（GET /observability/quality-summary）、`app/schemas/tasks.py` 扩展（Batch 视图）、metrics 扩展（llm/generation/batch 指标——V3B 预留的 REGISTRY 共享）。**LOCAL-DONE 红线**：生成链路用 adapter + mock HTTP transport（不触网）；Rubric 用 deterministic fake judge（R1 live 时 judge 真实调用）。

**Tech Stack:** Python 3.12、jsonschema（新依赖）、httpx、FastAPI、SQLAlchemy 2.0、pytest、ruff、mypy strict。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行（在 `main/` 下），禁止向 base/系统 Python 装依赖。
- 契约权威：structure-contract 3.7/4.2/6.9/6.10/8.3/8.4/8.5、database-design 2.5/2.7、PRD 5.7-5.11/AC-04/AC-07、openapi Batch/quality-summary 端点。实现不得修改 `docs/PRD/`、`docs/Architecture/`。
- **Schema 唯一入库门槛**（5.8/AC-04）：卡必须通过 `agent_evolution/schemas/v1/card.schema.json` 校验才入库；Rubric 不影响入库。**注意**：card.schema.json 是生成输出 schema（含 type/question/answer 或 type/statement/answer_boolean/explanation 结构）——校验器按资产加载；**Schema 校验失败 → 批次重试（最多 2 次重试共 3 次尝试）→ 仍失败 → 批次 SKIPPED 终态，任务继续**（4.2）。
- **分批**（4.2/5.7）：知识点按批分组（Settings `batch_size: int = 3`）；批次状态机 PENDING → PROCESSING → SUCCEEDED / FAILED →（重试 ≤2）→ SKIPPED；已完成批次（SUCCEEDED/SKIPPED）不重复执行；游标 completed_batch_count 与批次状态/卡片计数**同事务原子推进**。
- **生成调用**（V5A 真实 adapter + mock transport）：每批一次 chat 调用（prompt = 稳定前缀 + 动态后缀（知识点列表/配置/JSON schema 提示——V4 build_generation_prompt 复用）；响应 JSON 解析 → 逐卡 Schema 校验 → 合法卡入库（V1 模式 + generation_item_id 防重）→ 非法卡计数（批次仍可 SUCCEEDED 若有合法卡？——**裁决**：批次成功 = 至少一张合法卡入库；0 张合法卡 = 校验失败 → 重试；重试耗尽 → SKIPPED）。
- **usage 映射**（3.7/8.4）：provider 原样字段（usage.prompt_tokens/completion_tokens/prompt_cache_hit_tokens/prompt_cache_miss_tokens）与内部统一字段（cache_hit_tokens/cache_miss_tokens/output_tokens）双向映射受测；model/system_fingerprint/版本（prompt/schema/rubric_version）/request_id/duration_ms/http_status 落 Batch。
- **Rubric**（5.9/8.5）：4 维度（原文依据/准确性/清晰度/学习价值——以 rubric.md 资产为准）0-3 分总分 0-12；deterministic fake judge（本地规则：基于字段完整性/长度确定性打分）；分数落 Card（evidence/correctness/difficulty/learning_value/rubric_total_score）+ 批次质量（coverage/duplicate/distributions/difficulty_deviation）。
- **成本估算**（8.4）：价格配置常量（cache_hit/cache_miss/output 单价，标注生效日期）——`services/generation/cost.py` 常量 + `estimate_cost(usage, effective_date) -> float`；价格调整只改常量不动历史 token；估算仅聚合时计算（历史 token 原样）。
- **metrics**（8.3）：`llm_requests_total`（model/http_status）、`llm_request_duration_seconds`、`llm_tokens_total`（kind）、`generation_tasks_total`（result）、`generation_tasks_duration_seconds`、`batch_retry_total`——注册到 V3B 共享 REGISTRY（app/api/metrics.py 扩展）。
- **API**：GET /tasks/{task_id}/batches（Batch 列表——含 usage/版本/质量，AC-07 观测）；GET /observability/quality-summary（group_by model/pdf/difficulty；Rubric 平均/覆盖/重复率/任务完成率/成本汇总——隔离按 device_id）。
- 跨设备统一 404；时间格式唯一规范；错误响应 1.4 形状。
- 工作包边界：V5A 不含真实 DeepSeek 调用（R1 live）、checkpoint/resume 完整恢复（V5B）、单卡重写（V6）；`app/api/` 其他占位模块不得改动（tasks.py 已实现——扩展而非重写）。
- ruff line-length 100、mypy strict；四工具命令全绿；测试命名 `test_<模块>_<行为>`；提交只 `git add` 本任务文件。
- Task 1~5 由实现 subagent 完成；Task 6/7 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: 依赖 + Schema 校验器（card.schema.json）

**Files:**
- Modify: `main/pyproject.toml`（dependencies 加 `jsonschema>=4.20`）
- Modify: `main/requirements-dev.lock`
- Create: `main/services/generation/schema_validator.py`
- Create: `main/tests/unit/test_schema_validator.py`

**Interfaces:**
- Consumes: jsonschema（新依赖）、agent_evolution/schemas/v1/card.schema.json（只读）
- Produces: `services.generation.schema_validator.load_card_schema() -> dict`（资产加载 + JSON Schema 解析）；`services.generation.schema_validator.validate_card(card: dict, schema: dict) -> list[str]`（返回违约列表；空 = 合法）；Task 2 批处理消费

- [x] **Step 1: 安装 jsonschema + 核对 card.schema.json 结构**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend pip install "jsonschema>=4.20" && conda run -n shanka-backend pip-compile pyproject.toml --extra dev --output-file requirements-dev.lock`
然后核验：
```bash
conda run -n shanka-backend python -c "
import json
schema = json.load(open('/home/kbzz1/shanka_backend/agent_evolution/schemas/v1/card.schema.json'))
print('title:', schema.get('title'))
print('required:', schema.get('required'))
print('props:', list((schema.get('properties') or {}).keys()))
"
```
Expected: 记录 required/属性（type/question/answer/statement/answer_boolean/explanation 等）——校验器按实际结构实现。

- [x] **Step 2: 写失败单元测试 `main/tests/unit/test_schema_validator.py`**

```python
"""services.generation.schema_validator 单元测试（5.8 Schema 唯一入库门槛）。"""

import json

from services.generation.schema_validator import load_card_schema, validate_card


def test_schema_validator_question_card_valid() -> None:
    schema = load_card_schema()
    card = {"type": "QUESTION", "question": "什么是 FSRS？", "answer": "间隔重复算法"}
    assert validate_card(card, schema) == []


def test_schema_validator_true_false_card_valid() -> None:
    schema = load_card_schema()
    card = {"type": "TRUE_FALSE", "statement": "FSRS 是间隔重复算法", "answer_boolean": True, "explanation": "是"}
    assert validate_card(card, schema) == []


def test_schema_validator_missing_fields_invalid() -> None:
    schema = load_card_schema()
    violations = validate_card({"type": "QUESTION", "question": "q"}, schema)
    assert any("answer" in v for v in violations)


def test_schema_validator_wrong_types_invalid() -> None:
    schema = load_card_schema()
    violations = validate_card({"type": "QUESTION", "question": "q", "answer": 123}, schema)
    assert violations  # answer 类型非法
```

（说明：校验规则以 card.schema.json 实际结构为准——required 与属性类型从资产读取；测试断言按实际 schema 校准（如 type 枚举、answer 字符串）。）

- [x] **Step 3: 实现 `main/services/generation/schema_validator.py`**

```python
"""Schema 校验器（5.8/AC-04：Schema 是唯一入库门槛，Rubric 不影响入库）。

校验源：agent_evolution/schemas/v1/card.schema.json（manifest 唯一入口——infra/llm/prompts 的
load_asset("schemas", "card") 复用；资产演进 R-03 不原地改 v1）。
"""

import json

import jsonschema

from infra.llm.prompts import load_asset


def load_card_schema() -> dict:
    return json.loads(load_asset("schemas", "card"))


def validate_card(card: dict, schema: dict) -> list[str]:
    """校验卡片。返回违约列表（空 = 合法）。"""
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{err.json_path or err.path}: {err.message}" for err in validator.iter_errors(card)]
```

- [x] **Step 4: 运行确认通过 + ruff/mypy**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_schema_validator.py -v && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy services/generation/schema_validator.py tests/unit/test_schema_validator.py`
Expected: PASS（card.schema.json 的 schema 方言（draft）以实际为准——Draft202012 若与实际不兼容换 Draft7）

- [x] **Step 5: 提交**

```bash
git add main/pyproject.toml main/requirements-dev.lock main/services/generation/schema_validator.py main/tests/unit/test_schema_validator.py
git commit -m "feat(generation): JSON Schema 校验器（card.schema.json 资产加载）"
```

---

### Task 2: 分批执行核心（批次状态机/重试/游标/原子推进 + adapter 驱动）

**Files:**
- Modify: `main/app/config.py`（batch_size、generation_retry_limit）
- Create: `main/services/generation/batches.py`
- Modify: `main/services/tasks/executor.py`（V4 fake 执行 → V5A 分批 adapter 驱动）
- Create: `main/tests/integration/test_batches.py`
- Modify: `main/tests/integration/test_tasks_executor.py`（适配新执行）

**Interfaces:**
- Consumes: Task 1 schema、V3B adapter（DeepSeekClient + mock transport）、V4 prompts/planning、F1 models（Batch/KnowledgePoint）
- Produces: `services.generation.batches.plan_batches(session, *, task_id, knowledge_points) -> None`（按 batch_size 分组建 Batch PENDING + batch_index）；`services.generation.batches.process_next_batch(session, *, task_id, client) -> int`（取下一个 PENDING/FAILED（retry<2）批次 → PROCESSING → adapter.chat（prompt 组装）→ 解析卡片 → 逐卡 Schema 校验 → 合法卡入库（V1 模式 + generation_item_id 防重）→ 计数/质量 → SUCCEEDED（≥1 合法卡）或 FAILED（0 合法卡，retry+1；retry≥2 → SKIPPED）→ 游标 completed_batch_count 原子推进 → 返回处理数）；Settings 字段：`batch_size: int = 3`、`generation_retry_limit: int = 2`；executor 改为调用 process_next_batch 循环（每任务多批）；Task 3 rubric 与 Task 4 观测消费

- [x] **Step 1: 写失败集成测试 `main/tests/integration/test_batches.py`**

```python
"""分批生成集成测试：批次状态机/重试/游标/原子推进（真实 SQLite + mock transport）。"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from infra.db.models import Base, Batch, Card, KnowledgePoint, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.tasks.service import create_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'batches.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task_with_kps(session: Session, *, device_id: str, n_kps: int = 4) -> str:
    from infra.db.models import ApiKey, Chapter, PdfFile
    from services.decks.service import create_deck

    pdf = PdfFile(file_id=_uuid(), device_id=device_id, filename="b.pdf", storage_key=_uuid(), size_bytes=1, status="PARSED", created_at="2026-08-11T00:00:00.000Z")
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    session.add(ApiKey(device_id=device_id, encrypted_key="enc", status="AVAILABLE", masked_key="sk-****", updated_at="2026-08-11T00:00:00.000Z"))
    session.flush()
    task = create_task(session, device_id=device_id, file_id=pdf.file_id, deck_id=deck.deck_id,
                       chapter_ids=[ch.chapter_id],
                       config={"quantity_tendency": "COMPACT", "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2}},
                       now="2026-08-11T00:00:00.000Z")
    session.commit()
    return task.task_id


def _valid_cards_json(n: int = 2) -> str:
    cards = [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]
    return json.dumps({"cards": cards}, ensure_ascii=False)


def _client_ok(session_factory: Callable[[], Session]) -> DeepSeekClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": _valid_cards_json()}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8},
            "model": "deepseek-v4-flash",
        })

    return DeepSeekClient(Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(handler))


def test_batches_plan_and_process_all(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()
        total_batches = len(session.scalars(select(Batch).where(Batch.task_id == task_id)).all())
    assert total_batches >= 1
    client = _client_ok(session_factory)
    with session_factory() as session:
        processed = 0
        while True:
            n = process_next_batch(session, task_id=task_id, client=client)
            if n == 0:
                break
            session.commit()
            processed += n
    with session_factory() as session:
        task = session.get(Task, task_id)
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert processed == total_batches
    assert all(b.status == "SUCCEEDED" for b in batches)
    assert task.completed_batch_count == total_batches  # 游标原子推进
    assert task.total_batch_count == total_batches
    assert len(cards) > 0
    assert all(c.source == "GENERATED" for c in cards)


def test_batches_failed_batch_skipped_after_retries(session_factory: Callable[[], Session]) -> None:
    """非法输出（Schema 校验失败）→ 重试 2 次 → SKIPPED，任务继续（4.2）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"cards": [{"type": "QUESTION"}]}'}}]})

    client = DeepSeekClient(Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(bad_handler))
    with session_factory() as session:
        for _ in range(6):  # 多轮（每批最多 3 次尝试）
            if process_next_batch(session, task_id=task_id, client=client) == 0:
                break
            session.commit()
    with session_factory() as session:
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
        task = session.get(Task, task_id)
    assert all(b.status == "SKIPPED" for b in batches)
    assert all(b.retry_count == 2 for b in batches)  # 重试上限
    assert task.completed_batch_count == len(batches)  # SKIPPED 也推进游标


def test_batches_usage_and_versions_recorded(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()
    client = _client_ok(session_factory)
    with session_factory() as session:
        process_next_batch(session, task_id=task_id, client=client)
        session.commit()
    with session_factory() as session:
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).first()
    assert batch is not None
    assert batch.cache_hit_tokens == 2
    assert batch.cache_miss_tokens == 8
    assert batch.output_tokens == 5
    assert batch.model == "deepseek-v4-flash"
    assert batch.prompt_version == "v1" and batch.schema_version == "v1"
    assert batch.http_status == 200
```

（说明：批次生成的卡片难度/章节分布等质量字段 Task 3 填；usage/版本本任务填。`process_next_batch` 返回 0 = 无待处理批次。SKIPPED 的 retry_count=2（3 次尝试）。prompt_version/schema_version 从 asset_versions() 取。）

- [x] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_batches.py -v`
Expected: FAIL（ModuleNotFoundError）

- [x] **Step 3: 实现 `main/services/generation/batches.py` + executor 改造**

```python
"""services.generation.batches：分批执行核心（4.2 批次状态机/重试/游标原子推进）。

- plan_batches：知识点按 batch_size 分组建 Batch（PENDING）；
- process_next_batch：取下一个可处理批次（PENDING 或 FAILED 且 retry<limit）→ PROCESSING →
  adapter.chat（Prompt 组装）→ 响应 JSON 解析 → 逐卡 Schema 校验 → 合法卡入库 → SUCCEEDED/FAILED/SKIPPED →
  游标 completed_batch_count 原子推进（同事务）。
- Schema 是唯一入库门槛；Rubric 只观测（Task 3 接入）。
"""

import json
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from infra.db.models import Batch, Card, KnowledgePoint, ReviewState, Task
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import asset_versions, load_asset
from services.generation.schema_validator import load_card_schema, validate_card

logger = logging.getLogger(__name__)


def plan_batches(session: Session, *, task_id: str, knowledge_points: list[KnowledgePoint], batch_size: int = 3) -> None:
    for i in range(0, len(knowledge_points), batch_size):
        chunk = knowledge_points[i:i + batch_size]
        session.add(
            Batch(
                batch_id=str(uuid.uuid4()), task_id=task_id,
                batch_index=i // batch_size + 1, status="PENDING",
                generated_item_ids="[]", retry_count=0,
            )
        )


def _next_processable(session: Session, *, task_id: str, retry_limit: int) -> Batch | None:
    return session.scalar(
        select(Batch).where(
            Batch.task_id == task_id,
            (Batch.status == "PENDING") | (Batch.status == "FAILED"),
        ).order_by(Batch.batch_index).limit(1)
    )


def process_next_batch(session: Session, *, task_id: str, client: DeepSeekClient) -> int:
    settings = session.info.get("settings")  # 从 session.info 取（executor 注入）或参数传入——以实际实现为准
    batch = _next_processable(session, task_id=task_id, retry_limit=2)
    if batch is None:
        return 0
    batch.status = "PROCESSING"
    session.flush()
    task = session.get(Task, task_id)
    kps = session.scalars(
        select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
        .order_by(KnowledgePoint.priority).offset((batch.batch_index - 1) * 3).limit(3)
    ).all()
    # Prompt 组装（稳定前缀 + 动态后缀）
    prompt_asset = load_asset("prompts", "generator")
    card_schema = json.dumps(load_card_schema(), ensure_ascii=False)
    topic_list = "\n".join(f"- {kp.topic}" for kp in kps)
    prompt = f"{prompt_asset}\n本次批次知识点：\n{topic_list}\n请按 schema 输出：\n{card_schema}"
    try:
        result = client.chat(prompt, api_key="")  # api_key 由调用方注入（Task 4 从加密 Key 解密）——V5A 用占位
        ...
    ...
```

（说明：**Key 解密**——adapter.chat 需要真实 Key：executor 从 api_keys 表解密（crypto.decrypt_key，仅 infra/llm 路径）→ 传给 client。**实现细节**：process_next_batch 签名含 client（调用方已构造带 Key 的 client）；executor 负责解密 Key 构造 client。**批次响应解析**：content 为 JSON 字符串 → json.loads → cards 列表；无 cards 或全非法 → FAILED。**原子推进**：批次状态 + 卡入库 + task.completed_batch_count/count + kp 状态同事务。**难度/章节分布**：Task 3 rubric 填。完整实现按上述骨架 + 测试驱动修正。）

- [x] **Step 4: executor 改造（V4 fake → V5A adapter 分批）**

`executor._execute_task` 改为：plan_batches（若未建）→ 循环 process_next_batch（解密 Key 构造 client）→ 全部批次终态 → 任务 COMPLETED（或 FAILED 若系统级错误）。V4 fake 不再用于任务执行（样卡仍用 fake）。

- [x] **Step 5: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_batches.py tests/integration/test_tasks_executor.py -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿（test_tasks_executor 适配新执行——mock transport 注入）

- [x] **Step 6: 提交**

```bash
git add main/app/config.py main/services/generation/batches.py main/services/tasks/executor.py main/tests/integration/test_batches.py main/tests/integration/test_tasks_executor.py
git commit -m "feat(generation): 分批执行核心（批次状态机/重试/Schema 门槛/游标原子推进 + adapter 驱动）"
```

---

### Task 3: Rubric 观测（deterministic fake judge + 分数落库 + 批次质量）

**Files:**
- Create: `main/services/generation/rubric.py`
- Modify: `main/services/generation/batches.py`（批次成功时填质量/分数）
- Create: `main/tests/unit/test_rubric.py`

**Interfaces:**
- Consumes: rubric.md 资产（评分维度）、F1 models
- Produces: `services.generation.rubric.score_card(card: dict) -> dict`（deterministic：4 维度 0-3 分 + 总分 0-12——本地规则（字段完整/长度/类型一致性））；`services.generation.rubric.batch_quality(cards, total_kps, duplicated) -> dict`（coverage_rate/duplicate_rate/difficulty_distribution/chapter_distribution/card_type_distribution/difficulty_deviation）；Task 4 观测与批次列表消费

- [x] **Step 1: 写失败单元测试 `main/tests/unit/test_rubric.py`**

```python
"""services.generation.rubric 单元测试（5.9：4 维度 0-3 分总分 0-12，Rubric 不影响入库）。"""

from services.generation.rubric import batch_quality, score_card


def test_rubric_score_deterministic_and_in_range() -> None:
    card = {"type": "QUESTION", "question": "q" * 20, "answer": "a" * 20}
    s1 = score_card(card)
    s2 = score_card(card)
    assert s1 == s2  # deterministic
    for k in ("evidence_score", "correctness_score", "difficulty_score", "learning_value_score"):
        assert 0 <= s1[k] <= 3
    assert 0 <= s1["rubric_total_score"] <= 12


def test_rubric_score_different_for_different_cards() -> None:
    rich = {"type": "QUESTION", "question": "q" * 50, "answer": "a" * 50, "explanation": "e" * 30}
    poor = {"type": "QUESTION", "question": "q", "answer": ""}
    assert score_card(rich)["rubric_total_score"] >= score_card(poor)["rubric_total_score"]


def test_rubric_batch_quality_shape() -> None:
    cards = [
        {"type": "QUESTION", "target_difficulty": "BASIC", "chapter_id": "c1"},
        {"type": "TRUE_FALSE", "target_difficulty": "APPLICATION", "chapter_id": "c1"},
    ]
    q = batch_quality(cards, total_kps=3, duplicated=0)
    assert q["coverage_rate"] == 2 / 3
    assert q["duplicate_rate"] == 0.0
    assert q["difficulty_distribution"]["BASIC"] == 1
    assert q["card_type_distribution"]["QUESTION"] == 1
```

- [x] **Step 2: 实现 `main/services/generation/rubric.py`**

```python
"""Rubric 观测（5.9/8.5：4 维度 0-3 分总分 0-12；Rubric 不影响入库）。

LOCAL-DONE 用 deterministic fake judge（本地规则）；R1 live 用 LLM-as-judge
（scoring-prompt.md 资产）——fake 不代替生产（红线）。
"""


def score_card(card: dict) -> dict:
    """确定性评分：基于字段完整度/长度/一致性。返回 {4 维度分, rubric_total_score}。"""
    q_len = len(str(card.get("question") or card.get("statement") or ""))
    a_len = len(str(card.get("answer") or card.get("explanation") or ""))
    evidence = 3 if q_len >= 30 else (2 if q_len >= 10 else 1)
    correctness = 3 if a_len >= 30 else (2 if a_len >= 10 else (1 if a_len > 0 else 0))
    difficulty = 2 if card.get("target_difficulty") == "APPLICATION" else (1 if card.get("target_difficulty") == "UNDERSTANDING" else 0)
    learning = 3 if card.get("explanation") else 2
    total = evidence + correctness + difficulty + learning
    return {
        "evidence_score": evidence, "correctness_score": correctness,
        "difficulty_score": difficulty, "learning_value_score": learning,
        "rubric_total_score": total,
    }


def batch_quality(cards: list[dict], *, total_kps: int, duplicated: int) -> dict:
    """批次质量统计（5.10：覆盖率/重复率/难度/章节/类型分布/难度偏差——仅观测）。"""
    n = len(cards)
    return {
        "coverage_rate": n / total_kps if total_kps else 0.0,
        "duplicate_rate": duplicated / n if n else 0.0,
        "difficulty_distribution": _dist(cards, "target_difficulty"),
        "chapter_distribution": _dist(cards, "chapter_id"),
        "card_type_distribution": _dist(cards, "type"),
        "difficulty_deviation": 0.0,  # V5A 简化（观测字段结构完整）
    }


def _dist(cards: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for c in cards:
        v = str(c.get(key) or "unknown")
        out[v] = out.get(v, 0) + 1
    return out
```

- [x] **Step 3: batches.py 成功路径填质量/分数**

批次 SUCCEEDED 时：对每张合法卡 score_card → 分数落 Card 行（evidence/correctness/difficulty/learning_value/rubric_total_score）+ batch_quality → Batch 质量列。卡片需要 target_difficulty/chapter_id——批次内知识点决定（kp 的 difficulty 按批次内轮换、chapter_id=kp.chapter_id）。

- [x] **Step 4: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_rubric.py tests/integration/test_batches.py -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 5: 提交**

```bash
git add main/services/generation/rubric.py main/services/generation/batches.py main/tests/unit/test_rubric.py
git commit -m "feat(generation): Rubric 观测（deterministic fake judge + 分数/批次质量落库）"
```

---

### Task 4: 观测出口（批次列表 API + 质量聚合 API + metrics + 成本估算）

**Files:**
- Create: `main/services/generation/cost.py`
- Modify: `main/app/api/tasks.py`（GET /tasks/{task_id}/batches）
- Create: `main/app/api/observability.py`（GET /observability/quality-summary）
- Modify: `main/app/api/metrics.py`（llm/generation/batch 指标）
- Modify: `main/app/main.py`（装配 observability router）
- Create: `main/tests/integration/test_observability.py`、`main/tests/unit/test_cost.py`

**Interfaces:**
- Consumes: Task 2/3、metrics REGISTRY（V3B）
- Produces: `services.generation.cost.estimate_cost(cache_hit_tokens, cache_miss_tokens, output_tokens, effective_date) -> float`（价格常量按生效日期；单价示例：cache_hit ¥0.0000005/token、cache_miss ¥0.000002/token、output ¥0.000008/token——以 DeepSeek 官方定价近似标注生效日期，可替换）；`GET /tasks/{task_id}/batches`（Batch 视图列表：状态/retry/质量/usage/版本/model/http_status/duration/request_id——AC-07 观测）；`GET /observability/quality-summary?group_by=model|pdf|difficulty&days=30`（按 device 隔离聚合：Rubric 各维平均、覆盖/重复率均值、任务完成率、成本汇总）；metrics 扩展（llm_requests_total/llm_request_duration_seconds/llm_tokens_total/generation_tasks_total/generation_tasks_duration_seconds/batch_retry_total）；main.py 装配

- [x] **Step 1: 写失败单元测试 `main/tests/unit/test_cost.py`**

```python
"""services.generation.cost 成本估算单元测试（8.4：价格常量按生效日期，历史 token 不变）。"""

from services.generation.cost import estimate_cost


def test_cost_estimate_positive() -> None:
    cost = estimate_cost(cache_hit_tokens=1000, cache_miss_tokens=1000, output_tokens=500, effective_date="2026-08-11")
    assert cost > 0


def test_cost_estimate_uses_hit_and_miss_rates() -> None:
    a = estimate_cost(cache_hit_tokens=2000, cache_miss_tokens=0, output_tokens=0, effective_date="2026-08-11")
    b = estimate_cost(cache_hit_tokens=0, cache_miss_tokens=2000, output_tokens=0, effective_date="2026-08-11")
    assert b > a  # miss 单价 > hit 单价


def test_cost_estimate_zero_inputs() -> None:
    assert estimate_cost(0, 0, 0, effective_date="2026-08-11") == 0.0
```

- [x] **Step 2: 实现 `main/services/generation/cost.py`**

```python
"""成本估算（8.4/O-6）：价格配置常量（生效日期），历史 token 数据不变，调整只改常量。

单价近似（DeepSeek 官方定价量级，标注生效日期；可替换）：
- cache_hit: 0.5 元/百万 token；cache_miss: 2 元/百万；output: 8 元/百万（2026-08-11 起）。
"""

_PRICES = [
    {"effective_date": "2026-08-11", "cache_hit_per_token": 0.5 / 1_000_000, "cache_miss_per_token": 2.0 / 1_000_000, "output_per_token": 8.0 / 1_000_000},
]


def estimate_cost(*, cache_hit_tokens: int, cache_miss_tokens: int, output_tokens: int, effective_date: str) -> float:
    price = _PRICES[0]  # 生效日期匹配（当前单期；未来多期按日期选择）
    return round(
        cache_hit_tokens * price["cache_hit_per_token"]
        + cache_miss_tokens * price["cache_miss_per_token"]
        + output_tokens * price["output_per_token"],
        6,
    )
```

- [x] **Step 3: API 与 metrics**

`GET /tasks/{task_id}/batches`（handler：get_task 归属校验 → Batch 列表视图（含 cost 估算））；
`GET /observability/quality-summary`（handler：device 隔离 → SQL 聚合（group_by model/pdf/difficulty——按批次 model/章节/难度分布分组）→ Rubric 平均/覆盖/重复率均值/任务完成率（COMPLETED 任务数/总数）/成本汇总）；
metrics 扩展（app/api/metrics.py 增加 6 个指标对象 + executor/batches 上报点：llm_requests_total.labels(model, http_status)、llm_tokens_total.labels(kind)、batch_retry_total.inc()、generation_tasks_total.labels(result)、duration observe）。

- [x] **Step 4: 写失败集成测试 `main/tests/integration/test_observability.py`**

（批次列表含 usage/版本/质量；quality-summary 聚合；metrics 文本含 llm/generation/batch 指标——mock transport 驱动两批后断言）

- [x] **Step 5: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/unit/test_cost.py tests/integration/test_observability.py -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 6: 提交**

```bash
git add main/services/generation/cost.py main/app/api/tasks.py main/app/api/observability.py main/app/api/metrics.py main/app/main.py main/tests/integration/test_observability.py main/tests/unit/test_cost.py
git commit -m "feat(obs): 批次列表/质量聚合 API + llm/generation/batch 指标 + 成本估算"
```

---

### Task 5: acceptance AC-04/07 + 守卫

**Files:**
- Create: `main/tests/contract/test_batch_schemas_guard.py`
- Create: `main/tests/acceptance/test_acceptance_ac04_ac07.py`

**Interfaces:**
- Consumes: Task 1-4 全部产物
- Produces: AC-04（分批/Schema 门槛/Rubric 不影响入库）与 AC-07（Rubric+Cache 记录且不影响入库）验收映射；守卫（Batch ↔ openapi）

- [x] **Step 1: 守卫测试 `main/tests/contract/test_batch_schemas_guard.py`**

```python
"""契约守卫：Batch ↔ openapi（守卫 1 扩展）。"""

from app.schemas.tasks import Batch as BatchView
from tests.contract.support import check_schema_consistency, load_openapi, openapi_schema


def test_batch_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(BatchView, openapi_schema("Batch"), load_openapi())
    assert violations == []
```

（说明：Batch 视图模型按 openapi Batch required 集合定义；模型字段与 openapi 一致（守卫事实为准修正）。）

- [x] **Step 2: 验收测试 `main/tests/acceptance/test_acceptance_ac04_ac07.py`**

```python
"""验收测试：AC-04 正式生成与入库 + AC-07 质量与缓存数据（PRD；迁移 schema + HTTP + mock transport）。"""

# AC-04：任务分批生成 → 合法卡入库（Schema 通过）、Rubric 分数落库但入库由 Schema 决定
#   - mock transport 返回合法卡 → 任务 COMPLETED + cards 入库 + rubric 分数 > 0
#   - mock transport 返回非法卡 → 批次 SKIPPED（不入库）
# AC-07：Rubric 与 Cache 数据记录（批次列表含 usage/质量）且不影响入库规则
#   - GET /tasks/{id}/batches → items 含 cache/output tokens + rubric_total_score + 版本
```

- [x] **Step 3: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/contract/test_batch_schemas_guard.py tests/acceptance/ -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 4: 提交**

```bash
git add main/tests/contract/test_batch_schemas_guard.py main/tests/acceptance/test_acceptance_ac04_ac07.py
git commit -m "test(acceptance): AC-04/07 验收映射 + Batch 守卫"
```

---

### Task 6: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 V5A 产物；不新增代码

- [x] **Step 1: 四工具命令全绿**

Run（均在 `main/`）: `python --version`、`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`
Expected: 全绿

- [x] **Step 2: 干净环境安装 + 迁移**

（venv + alembic upgrade 验证）

- [x] **Step 3: uvicorn 冒烟（种子 → 任务 → 后台分批执行（mock 不可用于 uvicorn——**决策**：uvicorn 冒烟用 env 注入 mock transport？不——**LOCAL-DONE 红线**：不触网。**决策**：uvicorn 冒烟只验证不触网路径（healthz/批次列表空/质量聚合空态），分批执行由集成测试（mock transport）覆盖。**或**：Settings 加 `deepseek_mock_transport: bool = False`（测试/冒烟用）？——不引入生产 mock 配置。**决策**：uvicorn 冒烟 = 路由可达 + 空态；分批 mock 链路由测试证明。）**

```bash
cd /home/kbzz1/shanka_backend/main
rm -f shanka.db && conda run -n shanka-backend alembic -x database_url="sqlite:///./shanka.db" upgrade head
/home/kbzz1/miniconda3/envs/shanka-backend/bin/python -m uvicorn app.main:app --port 8085 > /tmp/v5a-uvicorn.log 2>&1 &
sleep 3
DEV=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
echo "healthz=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8085/healthz)"
echo "quality-summary=$(curl -s -H "X-Device-ID: $DEV" "http://127.0.0.1:8085/observability/quality-summary?days=30" | head -c 200)"
echo "metrics-llm=$(curl -s http://127.0.0.1:8085/metrics | grep -c 'llm_requests_total')"
kill %1
```
Expected: healthz 200、quality-summary 空态（has_data false 或空 items）、metrics 含 llm_requests_total

- [x] **Step 4: 关键边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_batches.py tests/integration/test_observability.py tests/acceptance/ tests/contract/ -v`
Expected: 全绿；记录关键用例名（重试→SKIPPED、游标推进、usage/版本、质量聚合、AC-04/07）

- [x] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app main/services main/infra --include="*.py" || true`
Expected: 无真实泄漏（测试假值除外）

---

### Task 7: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`
- Modify: `docs/superpowers/plans/2026-08-11-v5a-batched-generation-quality.md`（标题下「结果」）

- [x] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 V5A 行：`TODO` → `DONE`，证据填写：分批状态机/重试/Schema 门槛/游标原子推进、Rubric 观测、usage/版本/质量/成本/metrics、批次列表/质量聚合 API、AC-04/07 通过。
- 第 6 节：登记 R-15（4.1/4.4 PENDING vs RUNNING 契约文本同步——V5A 或 R1）与 V5A 相关裁决。
- 第 1 节状态基线：自动化验证测试数更新。
- 计划文件标题下「结果」注明 V5A DONE 与证据位置。

- [x] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-11-v5a-batched-generation-quality.md
git commit -m "docs(progress): V5A DONE（分批生成与质量观测闭环），AC-04/07 通过"
```

---

## Self-Review

**1. Spec coverage（对照 Progress V5A 文本）：**

| V5A 要求 | 落点 |
| --- | --- |
| 按知识点分批 | Task 2 plan_batches（batch_size） |
| 每批最多 2 次重试 | Task 2（retry_count<2 → FAILED 重试 → SKIPPED） |
| Schema 唯一入库门槛 | Task 1/2（card.schema.json + validate_card；Rubric 不影响） |
| Rubric 只观测 | Task 3（score_card/batch_quality——fake judge） |
| 合法卡/generation_item_id/知识点/批次状态/游标/计数原子推进 | Task 2（同事务） |
| 失败批次 SKIPPED 后继续 | Task 2（SKIPPED 终态 + 游标推进 + 任务继续） |
| 记录 model/system_fingerprint/usage/版本 | Task 2（Batch 列 + asset_versions） |
| Prompt/Cache token | Task 2（usage 映射） |
| 批次/质量聚合/指标/成本估算 | Task 4 |
| provider usage 原样与内部统一字段映射受测 | Task 2 测试（usage 断言） |
| 价格调整不改历史 token | Task 4（cost 只算聚合，token 原样） |
| 版本/缓存/质量/成本可核验 | Task 4/5 |
| AC-04/07 | Task 5 |

**2. Placeholder scan：** 全部任务给出完整文件内容与可执行命令。Task 2 的 process_next_batch 骨架含省略号（Key 注入/响应解析/原子推进细节）——实现者按测试驱动补全（测试已定义行为契约），报告记录；Task 6 冒烟决策（mock 不入 uvicorn）明确。非占位。

**3. Type consistency：** `load_card_schema() -> dict`/`validate_card(card, schema) -> list[str]`（Task 1 定义，Task 2 使用）；`plan_batches(session, *, task_id, knowledge_points, batch_size=3)`/`process_next_batch(session, *, task_id, client) -> int`（Task 2 定义，executor 使用）；`score_card(card) -> dict`/`batch_quality(cards, *, total_kps, duplicated) -> dict`（Task 3 定义，Task 2/4 使用）；`estimate_cost(*, cache_hit_tokens, cache_miss_tokens, output_tokens, effective_date) -> float`（Task 4 定义，质量聚合使用）；`load_asset`/`asset_versions`（V4 定义，Task 1/2 使用）；`DeepSeekClient.chat(prompt, api_key)`（V3B 定义，Task 2 使用）。
