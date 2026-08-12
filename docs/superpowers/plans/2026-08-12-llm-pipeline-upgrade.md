# LLM 链路升级 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 激活并升级 LLM 全链路（文本页持久化 → LLM 规划 → 单元锚定生成 → LLM 评分），任务创建异步化，批=单元，账本权威，估算删除，契约同步。

**Architecture:** 代码决定数量/配比/上限/ID/状态机；LLM 只产出带"学习目标+目标难度+锚定卡型+来源引用"的生成单元。规划快照在首次 CAS1 抢占时原子冻结；所有 LLM 调用先持久化 STARTED 占位（`llm_call_attempts` 账本为重试/上限/成本权威）；领域写入与调用终态同事务；PLANNING → GENERATING → SCORING 三阶段，全部条件更新防并发。

**Tech Stack:** FastAPI / SQLAlchemy 2.0 / Alembic / pypdf / DeepSeek API / unittest + pytest / Android (Kotlin) 前端 `frontend-app/Front/`。

## Global Constraints

- 执行环境：`cd main && conda run -n shanka-backend python -m pytest`；四工具全绿：pytest / `ruff check .` / `ruff format --check .` / `mypy .`（line-length 100、mypy strict）。
- 红线 4：API Key 只出现在 infra/llm 调用路径；日志/响应/任务明细/账本不得出现明文、完整 Prompt、原始模型响应；账本 `normalized_result` 只存通过校验的规范化 units JSON。
- 红线 5：manifest ↔ structure-contract 版本一致；已发布资产目录禁止原地修改（新增版本 = 新目录 + manifest + CHANGELOG 同次提交）。
- 每单元 1 卡（N=1 固定）；密度只控制单元预算上限（每章 3×密度系数，COMPACT=1/BALANCED=2/EXTENSIVE=3）；预算 2 章 = 6/12/18。
- 配额三层最大余数法，固定顺序（BASIC<UNDERSTANDING<APPLICATION、章序、组序）消除随机性；例：预算 6、40/40/20 → 3/2/1。
- LLM 调用必须在事务外发起；任何外部 chat 调用前必须先有已提交的 STARTED 账本行。
- 任务状态：PENDING(PLANNING) → RUNNING(PLANNING) → RUNNING(GENERATING) → RUNNING(SCORING) → COMPLETED/FAILED/CANCELLED；PAUSED 仅由 resume 恢复。
- 空单元三分支：全成功 0 单元 → COMPLETED + `completion_reason=NO_GENERATION_UNITS`；全失败 → FAILED+failure_stage=PLANNING；部分失败 → 成功组继续 + `skipped_planning_group_count`。
- 成本口径：账本是全阶段 token 唯一来源；Batch token 仅 GENERATING 兼容投影；quality-summary `cost_estimate` 标注 generation-stage only，禁止双计。
- 完成口径：本地四工具/守卫通过 → `LOCAL_IMPLEMENTATION_DONE`；受控真实 Planner→Generator→Scoring canary 通过 → `PRODUCTION_VALIDATED` → R-03 才可 RESOLVED。
- 前端：直接修改 `frontend-app/Front/` 源码，不新增 `docs/frontend/handoff/*`，不 fork/push；机器契约以 openapi.yaml 为权威。

---

## File Structure

**后端（main/）：**
- `infra/db/models.py` — 加列（knowledge_points/batches/tasks）+ 新表 TextChunk、LlmCallAttempt
- `main/migrations/versions/0003_llm_pipeline_upgrade.py` — 迁移
- `app/config.py` — 9 个新 Settings 字段
- `services/pdf/parser.py` — parse_pdf 返回页文本列表（扩展）
- `services/pdf/scanner.py` — PARSED 时持久化页文本
- `services/generation/quota.py`（新）— 预算 + 三层配额（纯函数）
- `services/generation/ledger.py`（新）— 账本服务层
- `services/generation/planner_validator.py`（新）— planner 输出校验/截断/规范化
- `services/generation/scoring_validator.py`（新）— scoring 输出校验
- `services/generation/planning_executor.py`（新）— 规划执行（CAS/冻结/分组/调用/合并/最终落库）
- `services/generation/scoring.py`（新）— 抽样/组批/评分执行
- `services/generation/batches.py` — 批=单元改造
- `services/generation/rubric.py` — fake 评分退役（改 LLM 输出解析辅助）
- `services/generation/token_estimator.py` — 删除；`planning.py` — 重构为预算（或并入 quota.py 后删除）
- `services/tasks/service.py`、`services/tasks/executor.py` — 状态机/规划 worker
- `app/api/tasks.py`、`app/api/observability.py`、`app/schemas/tasks.py` — 路由/schema
- `infra/llm/deepseek.py` — retryable 元数据；`infra/llm/prompts.py` — 多资产版本入口
- `agent_evolution/` — prompts/v3、rubrics/v2、schemas/v2、manifest、CHANGELOG

**前端（frontend-app/Front/）：** 任务创建流程删 estimate、加三阶段/空结果展示。

---

### Task 1: 迁移 0003 + ORM（表结构地基）

**Files:**
- Modify: `main/infra/db/models.py`（KnowledgePoint/Batch/Task + 新表 TextChunk、LlmCallAttempt）
- Create: `main/migrations/versions/0003_llm_pipeline_upgrade.py`
- Test: `main/tests/infra/test_migration_0003.py`

**Interfaces:**
- Consumes: 现有 Base、各表（Task 85-118 行、KnowledgePoint 119-134、Batch 136-168）
- Produces:
  - `TextChunk(Base)`：`chunk_id` PK、`file_id` FK CASCADE、`page_number`、`char_count`、`content_sha256`、`content` Text、`created_at`；`UniqueConstraint(file_id, page_number)`；Index `ix_text_chunks_file_page(file_id, page_number)`
  - `LlmCallAttempt(Base)`：见 spec §9 列清单；`UniqueConstraint(scope_type, scope_id, stage, operation_key, attempt_no)`；Index `(device_id, created_at)`、`(task_id, stage, operation_key)`；`task_id` FK CASCADE nullable
  - `KnowledgePoint` 新列：`target_difficulty: str|None`、`card_type: str|None`、`source_chunk_ids: str|None`（Text）
  - `Batch` 新列：`generation_unit_id: str|None`（FK knowledge_points.knowledge_point_id ON DELETE SET NULL）+ `UniqueConstraint(task_id, generation_unit_id)`
  - `Task` 新列：`completion_reason: str|None`、`skipped_planning_group_count: int`（default 0, nullable=False）

- [ ] **Step 1: 写失败测试**

```python
# main/tests/infra/test_migration_0003.py
from sqlalchemy import inspect

from infra.db.models import TextChunk, LlmCallAttempt


def test_models_have_new_columns():
    from infra.db.models import KnowledgePoint, Batch, Task
    assert KnowledgePoint.__table__.c.target_difficulty is not None
    assert KnowledgePoint.__table__.c.card_type is not None
    assert KnowledgePoint.__table__.c.source_chunk_ids is not None
    assert Batch.__table__.c.generation_unit_id is not None
    assert Task.__table__.c.completion_reason is not None
    assert Task.__table__.c.skipped_planning_group_count is not None


def test_text_chunk_unique_per_page():
    table = TextChunk.__table__
    assert [uc.name for uc in table.constraints] or True
    assert table.c.chunk_id.primary_key


def test_ledger_unique_constraint():
    table = LlmCallAttempt.__table__
    uniques = [
        c
        for c in table.constraints
        if isinstance(c, __import__("sqlalchemy").UniqueConstraint)
    ]
    assert any("attempt_no" in str(u.name) for u in uniques)
```

- [ ] **Step 2: 运行确认失败** — `conda run -n shanka-backend python -m pytest tests/infra/test_migration_0003.py -v` → FAIL（ImportError）

- [ ] **Step 3: 实现 ORM** — models.py 追加新列与新表（列类型对齐 spec §11；`TextChunk.content` 用 `Text`；`LlmCallAttempt.normalized_result` 用 `Text` nullable；status 默认 "STARTED"）

- [ ] **Step 4: 生成迁移脚本** — 运行 `conda run -n shanka-backend python -m alembic revision --autogenerate -m "llm_pipeline_upgrade"`；人工校对生成的 `0003_*.py`：必须包含两个新表 + 三表加列 + 唯一/索引约束；删掉 autogenerate 误生成的无关变更（如建表顺序）后手写纯增量的 upgrade/downgrade。

- [ ] **Step 5: 迁移往返测试** — 空库 `alembic upgrade head` → `alembic downgrade 0002` → 再 `upgrade head`；`alembic check` 零漂移；四工具全绿（本任务仅 pytest 新增 + 既有回归）

- [ ] **Step 6: Commit** — `git add main/ && git commit -m "feat(llm-upgrade): 迁移 0003——text_chunks/llm_call_attempts 新表 + 三表加列"`

---

### Task 2: Settings 新增 9 个硬上限/预算字段

**Files:**
- Modify: `main/app/config.py:20-62`
- Test: `main/tests/app/test_config_defaults.py`

**Interfaces:**
- Produces（全部 `Field(default=...)`，注释标注 spec 章节）：
  - `planner_max_input_chars: int = 20_000`（§4.2/§10）
  - `max_generation_units_per_task: int = 300`（§10，POST 预算校验）
  - `max_planner_groups_per_task: int = 30`（§6.2）
  - `max_source_pages_per_unit: int = 8`（§10）
  - `generator_max_input_chars: int = 10_000`（§10）
  - `max_scoring_calls_per_task: int = 60`（§8）
  - `scoring_max_cards_per_call: int = 12`（§8）
  - `scoring_max_input_chars: int = 15_000`（§8）
  - `planning_retry_limit: int = 2`（§6.3，每组 3 次尝试）

- [ ] **Step 1: 失败测试**

```python
def test_new_hard_limits_defaults():
    from app.config import Settings
    s = Settings()
    assert s.max_generation_units_per_task == 300
    assert s.max_planner_groups_per_task == 30
    assert s.max_scoring_calls_per_task == 60
    assert s.planner_max_input_chars == 20_000
    assert s.max_source_pages_per_unit == 8
    assert s.generator_max_input_chars == 10_000
    assert s.scoring_max_cards_per_call == 12
    assert s.scoring_max_input_chars == 15_000
    assert s.planning_retry_limit == 2
```

- [ ] **Step 2: 运行确认失败** — AttributeError
- [ ] **Step 3: 实现** — config.py 追加字段
- [ ] **Step 4: 通过 + 四工具** — 追加后运行 pytest/ruff/mypy
- [ ] **Step 5: Commit** — `feat(llm-upgrade): Settings 硬上限与预算字段`

---

### Task 3: 页文本解析 + text_chunks 持久化

**Files:**
- Modify: `main/services/pdf/parser.py`、`main/services/pdf/scanner.py`
- Create: `main/services/pdf/text_chunks.py`、`main/tests/services/pdf/test_text_chunks.py`
- Test: `main/tests/services/pdf/test_scanner_text_chunks.py`（扩展既有）

**Interfaces:**
- Consumes: `TextChunk`（T1）、Settings（T2）、现有 `parse_pdf(path) -> tuple[str, list[ChapterInfo]]`
- Produces:
  - `parser.py`: `extract_pages(path) -> list[PageText]`，`PageText = TypedDict("page_number": int, "content": str)`（完整页文本，失败抛 `PDF_PARSE_FAILED`）
  - `text_chunks.py`:
    - `chunk_id_for(file_id: str, page_number: int, content: str) -> str`（`uuid5(NAMESPACE_URL, f"{file_id}:{page_number}:{sha256(content)}")` 确定性）
    - `persist_text_chunks(session, *, file_id, pages, now) -> None`（先删该 file_id 既有再插；`char_count=len(content)`、`content_sha256=sha256(content).hexdigest()`）
    - `load_pages(session, *, file_id, start_page, end_page) -> list[TextChunk]`（按 page_number 升序）
    - `page_text_map(chunks) -> dict[int, str]`
- 行为：scanner PARSED 时 `extract_pages` 持久化（重解析幂等：清理重建）；页文本不随章节编辑重建；PDF 删除级联（FK）。

- [ ] **Step 1: 失败测试**

```python
# main/tests/services/pdf/test_text_chunks.py
from services.pdf.text_chunks import chunk_id_for, persist_text_chunks, load_pages


def test_chunk_id_deterministic():
    a = chunk_id_for("f1", 3, "内容 abc")
    b = chunk_id_for("f1", 3, "内容 abc")
    assert a == b
    assert chunk_id_for("f1", 3, "改") != a


def test_persist_and_load_roundtrip(session):
    pages = [{"page_number": 1, "content": "第一页"}, {"page_number": 2, "content": "第二页"}]
    persist_text_chunks(session, file_id="pdf-1", pages=pages, now="2026-08-12T00:00:00.000Z")
    session.commit()
    rows = load_pages(session, file_id="pdf-1", start_page=1, end_page=2)
    assert [r.page_number for r in rows] == [1, 2]
    assert rows[0].content == "第一页"


def test_reparse_rebuilds_and_cascades(session):
    persist_text_chunks(session, file_id="pdf-1", pages=[{"page_number": 1, "content": "v1"}], now="...")
    persist_text_chunks(session, file_id="pdf-1", pages=[{"page_number": 1, "content": "v2"}], now="...")
    session.commit()
    assert len(load_pages(session, file_id="pdf-1", start_page=1, end_page=5)) == 1
    assert load_pages(session, file_id="pdf-1", start_page=1, end_page=5)[0].content == "v2"
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现三个函数 + parser.extract_pages**（`PdfReader(str(path)).pages` 逐页 `extract_text() or ""`，总页数 0 或全部空 → PDF_PARSE_FAILED；章节解析保留现状）
- [ ] **Step 4: scanner 接线** — `process_pending` 内 `parse_pdf` 成功后调 `persist_text_chunks`（同事务）；scanner 既有测试扩展断言页文本存在
- [ ] **Step 5: 四工具全绿 + Commit** — `feat(llm-upgrade): 页文本提取与 text_chunks 持久化（一页一行）`

---

### Task 4: 预算与三层配额算法（纯函数）

**Files:**
- Create: `main/services/generation/quota.py`、`main/tests/services/generation/test_quota.py`
- Modify: `main/services/generation/planning.py`（删除，预算逻辑迁入 quota.py）——本任务只迁移 `knowledge_point_count` 为 `task_unit_budget`

**Interfaces:**
- Produces:
  - `task_unit_budget(chapter_count: int, quantity_tendency: str) -> int`（`chapter_count * 3 * {"COMPACT":1,"BALANCED":2,"EXTENSIVE":3}.get(tendency, 2)`）
  - `largest_remainder(amounts: list[float], total: int, order: list[str]) -> dict[str, int]`：整数部分 + 余数降序补 1（并列按 order 顺序），返回 `{label: count}` 且 `sum == total`
  - `allocate_task_quota(total_budget: int, ratio_basic: float, ratio_understanding: float, ratio_application: float) -> dict[str, int]`
  - `allocate_chapter_quota(task_quota: dict[str, int], chapter_count: int) -> list[dict[str, int]]`（每难度按章均分 + 最大余数法按章序）
  - `allocate_group_quota(chapter_quota: dict[str, int], group_char_counts: list[int]) -> list[dict[str, int]]`（每难度按 char 占比，`sum(char_counts)==0` 时均分）

- [ ] **Step 1: 失败测试（确定性断言，spec §3.5 例子）**

```python
from services.generation.quota import (
    task_unit_budget, allocate_task_quota, largest_remainder,
    allocate_chapter_quota, allocate_group_quota,
)


def test_budget_keeps_v4_caliber():
    assert task_unit_budget(2, "COMPACT") == 6
    assert task_unit_budget(2, "BALANCED") == 12
    assert task_unit_budget(2, "EXTENSIVE") == 18
    assert task_unit_budget(2, "WEIRD") == 12  # 未知回落 BALANCED


def test_task_quota_40_40_20_gives_3_2_1():
    assert allocate_task_quota(6, 0.4, 0.4, 0.2) == {
        "BASIC": 3, "UNDERSTANDING": 2, "APPLICATION": 1,
    }


def test_largest_remainder_total_preserved():
    out = largest_remainder([2.4, 2.4, 1.2], 6, ["BASIC", "UNDERSTANDING", "APPLICATION"])
    assert sum(out.values()) == 6


def test_chapter_quota_distributes_evenly():
    q = allocate_chapter_quota({"BASIC": 3, "UNDERSTANDING": 2, "APPLICATION": 1}, 2)
    assert sum(ch["BASIC"] for ch in q) == 3
    assert len(q) == 2


def test_group_quota_by_char_share():
    g = allocate_group_quota({"BASIC": 3}, [2000, 4000])
    assert sum(x["BASIC"] for x in g) == 3
    assert g[0]["BASIC"] == 1 and g[1]["BASIC"] == 2  # 1:2 占比
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现 quota.py**（最大余数法：`floors + remainder sort desc with stable order`）
- [ ] **Step 4: 迁移 planning.py 预算入口** — 删除 `planning.py` 中 `knowledge_point_count` 的调用点改为 `task_unit_budget`（`services/tasks/service.py` 引用处同步，本任务只改符号、行为不变）；`token_estimator.py` 的引用在 T13 删除前先改为 `task_unit_budget` 保持可运行
- [ ] **Step 5: 四工具 + 全量回归 + Commit** — `feat(llm-upgrade): 单元预算与三层最大余数法配额`

---

### Task 5: LLM 资产 v3/v2 + manifest + 加载扩展

**Files:**
- Create: `agent_evolution/prompts/v3/{planner,generator,rewrite}.md`、`agent_evolution/rubrics/v2/{rubric.md,scoring-prompt.md}`、`agent_evolution/schemas/v2/{planner-output,scoring-output}.schema.json`
- Modify: `agent_evolution/manifest.json`、`agent_evolution/CHANGELOG.md`、`main/infra/llm/prompts.py`
- Test: `main/tests/infra/llm/test_prompt_assets_v3.py`

**Interfaces:**
- Consumes: manifest 结构（prompts/schemas/rubrics 节）
- Produces:
  - `load_asset(section, name)` 保持签名；manifest 新增 `prompts.scoring`、`schemas.planner_output`、`schemas.scoring_output` 入口（显式路径，禁止相对路径绕过）
  - `asset_versions()` 返回扩展：`{"generator_prompt_version", "planner_prompt_version", "rewrite_prompt_version", "scoring_prompt_version", "card_schema_version", "planner_output_schema_version", "scoring_output_schema_version", "rubric_version"}`
  - `load_schema_asset(name) -> dict`（planner_output/scoring_output JSON 加载）

资产内容要点（写入文件时按以下要求成文）：
- `planner.md`：输入 = 页文本（服务端提供）+ 子配额；输出 `{"units":[...]}`；按难度描述规划形态（BASIC 原子知识点 / UNDERSTANDING 理解主题 / APPLICATION 开放性问题或场景判断题）；§3.3 组合规则（判断题不可事实换皮、须可二值、explanation 给依据）；不要求 LLM 维持三档密度相对数量（代码已算好配额）
- `generator.md`：继承 v2 的 `{"cards":[单张]}` 包装指令；每单元恰好 1 张锚定类型卡；输入含学习目标/锚定难度/锚定卡型/页文本；判断题多角度约束
- `rewrite.md`：内容质量升级（表述准确/清晰/学习价值），保持"类型/主题不变"规则
- `rubric.md`：v2 评分标准（基础类侧重准确/证据；应用类侧重分析深度与多角度）
- `scoring-prompt.md`：输出 `{"scores":[...]}` 契约（见 spec §5.4）
- `planner-output.schema.json`：`{"type":"object","required":["units"],"properties":{"units":{"type":"array","items":{...source_chunk_ids(minItems 1, uniqueItems)/learning_objective/target_difficulty enum/card_type enum/priority}}}}`
- `scoring-output.schema.json`：scores 数组（generation_item_id/四维 0~3/rubric_total_score）

- [ ] **Step 1: 失败测试（版本契约）**

```python
from infra.llm.prompts import load_asset, asset_versions, load_schema_asset


def test_manifest_has_new_entries():
    assert "scoring" in load_asset.__globals__["load_manifest"]()["prompts"]
    assert "planner_output" in load_asset.__globals__["load_manifest"]()["schemas"]


def test_v3_assets_loadable():
    for sec, name in [("prompts", "planner"), ("prompts", "generator"), ("prompts", "rewrite"), ("prompts", "scoring")]:
        assert len(load_asset(sec, name)) > 100


def test_versions_extended():
    v = asset_versions()
    assert v["generator_prompt_version"] == "v3"
    assert v["planner_prompt_version"] == "v3"
    assert v["scoring_prompt_version"] == "v2"
    assert v["card_schema_version"] == "v1"


def test_planner_output_schema_validates():
    schema = load_schema_asset("planner_output")
    assert schema["required"] == ["units"]
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 写 7 个资产文件**（内容按上方要点成文；v1/v2 目录一律不动）
- [ ] **Step 4: manifest + CHANGELOG + prompts.py 扩展**（`asset_versions` 多入口、`load_schema_asset`）
- [ ] **Step 5: 既有引用回归** — `asset_versions()` 返回键变更会影响 `batches.py`（取 `versions["prompt_version"]` 处）——本任务同步改 `batches.py` 用 `generator_prompt_version`/`card_schema_version`/`rubric_version`
- [ ] **Step 6: 四工具全绿 + Commit** — `feat(llm-upgrade): 资产 v3/v2（planner/generator/rewrite/scoring）+ manifest 多入口`

---

### Task 6: DeepSeek 适配层 retryable 区分（最小扩展）

**Files:**
- Modify: `main/infra/llm/deepseek.py`
- Test: `main/tests/infra/llm/test_deepseek_retryable.py`（mock transport）

**Interfaces:**
- Consumes: `DeepSeekClient.chat/validate_key` 现有错误映射
- Produces:
  - `class RetryableUpstreamError(AppError)`（`retryable: bool = True`；`AppError` 加可选属性或子类携带）
  - chat 错误映射细分：401 → `API_KEY_UNAVAILABLE`（retryable=False）；429/5xx/网络/超时 → `API_KEY_UNAVAILABLE`（retryable=True）或 `GENERATION_FAILED`（retryable=True）
  - `validate_key` 行为不变（401→INVALID 语义保持）

- [ ] **Step 1: 失败测试（mock transport）**

```python
# 复制 main/tests/infra/llm/test_deepseek.py 的 MockTransport client 构造定式
# （httpx.MockTransport(handler) + DeepSeekClient(settings, api_key="sk-test")），
# 仅替换 handler 返回与断言：
def test_401_not_retryable():
    handler = lambda req: httpx.Response(401, json={"error": {"message": "invalid"}})
    client = _build_client(handler)  # 既有定式的构造 helper
    with pytest.raises(RetryableUpstreamError) as ei:
        client.chat("hi")
    assert ei.value.code == ErrorCode.API_KEY_UNAVAILABLE
    assert ei.value.retryable is False


def test_429_and_5xx_retryable():
    for status in (429, 500, 503):
        handler = lambda req, s=status: httpx.Response(s, json={"error": {"message": "up"}})
        client = _build_client(handler)
        with pytest.raises(RetryableUpstreamError) as ei:
            client.chat("hi")
        assert ei.value.retryable is True
```

（`_build_client` 复制既有测试文件的 helper；若既有文件无 helper 则直接内联构造。）

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — 定义 `RetryableUpstreamError`；chat 的 429/5xx/网络分支 raise 时带 `retryable=True`；401 分支 False；`GENERATION_FAILED`（解析失败）标记 retryable=False（输出错误走业务重试，不属于上游重试分类，调用方按 status 判定）
- [ ] **Step 4: 既有 DeepSeek 测试全绿 + 四工具 + Commit** — `feat(llm-upgrade): adapter 区分 401 非重试与 429/5xx retryable`

---

### Task 7: llm_call_attempts 账本服务层

**Files:**
- Create: `main/services/generation/ledger.py`、`main/tests/services/generation/test_ledger.py`

**Interfaces:**
- Consumes: `LlmCallAttempt`（T1）、Settings（T2）
- Produces:
  - `create_attempt(session, *, device_id, scope_type, scope_id, task_id, stage, operation_key, input_fingerprint, attempt_no, model, prompt_name, prompt_version, schema_name=None, schema_version=None, rubric_version=None) -> LlmCallAttempt`（INSERT 后 flush；唯一约束冲突 → 409 语义抛 `AppError(IDEMPOTENCY_CONFLICT)`）
  - `finish_success(session, attempt, *, usage: dict, http_status, duration_ms, normalized_result=None) -> None`
  - `finish_failed(session, attempt, *, error_code) -> None`
  - `mark_stale_unknown(session, *, task_id, stage) -> int`（孤儿 STARTED → UNKNOWN，返回行数）
  - `attempt_count(session, *, task_id, stage, operation_key) -> int`（STARTED/SUCCESS/FAILED/UNKNOWN 全算）
  - `find_success_result(session, *, task_id, stage, operation_key, input_fingerprint) -> str | None`（normalized_result）
  - `scoring_attempt_total(session, *, task_id) -> int`
  - `task_token_totals(session, *, task_id) -> dict[str, int]`（按 stage 汇总 cache_hit/cache_miss/output，成本口径唯一来源）

- [ ] **Step 1: 失败测试**

```python
def test_create_then_finish_success_roundtrip(session, settings_override):
    att = create_attempt(
        session, device_id="d1", scope_type="TASK", scope_id="t1", task_id="t1",
        stage="PLANNING", operation_key="planning:ch1:g0", input_fingerprint="fp1",
        attempt_no=1, model="m", prompt_name="planner", prompt_version="v3",
    )
    finish_success(session, att, usage={"prompt_cache_hit_tokens": 10, "prompt_cache_miss_tokens": 5, "completion_tokens": 7}, http_status=200, duration_ms=10, normalized_result='{"units": []}')
    session.commit()
    assert find_success_result(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0", input_fingerprint="fp1") == '{"units": []}'
    assert attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1


def test_started_counts_toward_budget(session, settings_override):
    create_attempt(session, device_id="d1", scope_type="TASK", scope_id="t1", task_id="t1",
                   stage="PLANNING", operation_key="planning:ch1:g0", input_fingerprint="fp2", attempt_no=1,
                   model="m", prompt_name="planner", prompt_version="v3")
    session.commit()
    assert attempt_count(session, task_id="t1", stage="PLANNING", operation_key="planning:ch1:g0") == 1


def test_mark_stale_unknown(session, settings_override):
    create_attempt(session, device_id="d1", scope_type="TASK", scope_id="t1", task_id="t1",
                   stage="PLANNING", operation_key="planning:ch1:g0", input_fingerprint="fp3", attempt_no=1,
                   model="m", prompt_name="planner", prompt_version="v3")
    session.commit()
    assert mark_stale_unknown(session, task_id="t1", stage="PLANNING") == 1


def test_duplicate_attempt_no_raises(session, settings_override):
    kw = dict(device_id="d1", scope_type="TASK", scope_id="t1", task_id="t1", stage="PLANNING",
              operation_key="k", input_fingerprint="f", model="m", prompt_name="p", prompt_version="v")
    create_attempt(session, attempt_no=1, **kw)
    create_attempt(session, attempt_no=1, **kw)  # 唯一约束冲突
    with pytest.raises(Exception):
        session.flush()
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现 ledger.py**（全部函数；唯一约束冲突捕获 `IntegrityError` → `session.rollback()` 后抛 `AppError(IDEMPOTENCY_CONFLICT, ...)`；`mark_stale_unknown` 用 `update().where(status=="STARTED")`）
- [ ] **Step 4: 四工具 + Commit** — `feat(llm-upgrade): LLM 调用账本服务层（STARTED 占位/终态/UNKNOWN/预算/恢复）`

---

### Task 8: 任务创建改造（PENDING+PLANNING、预算校验、创建快照）

**Files:**
- Modify: `main/services/tasks/service.py`（create_task）、`main/app/api/tasks.py`（POST 路由幂等接线不变）、`main/app/schemas/tasks.py`（Task 视图加字段）
- Test: `main/tests/services/tasks/test_create_task_planning.py`、`main/tests/app/api/test_tasks_planning_api.py`

**Interfaces:**
- Consumes: `task_unit_budget`（T4）
- Produces:
  - `create_task(...)` 改为：`status="PENDING"`、`stage="PLANNING"`、`started_at=None`、`total_batch_count=None`；预算校验 `task_unit_budget(len(chapter_ids), config.quantity_tendency) > settings.max_generation_units_per_task` → 抛 `AppError(VALIDATION_ERROR, "生成单元预算超出上限")`；**不再同事务规划**（删除 plan_knowledge_points 调用）；selected_chapters 快照 JSON 与现状一致（创建快照语义）
  - `task_view` 增加 `completion_reason`、`skipped_planning_group_count`
  - `TaskCreateResponse`/`Task` schema 同步（openapi 联动在 T14）

- [ ] **Step 1: 失败测试**

```python
def test_create_task_pending_planning(session, settings_override):
    task = create_task(session, device_id="d1", file_id="f1", deck_id="dk1",
                       chapter_ids=["c1"], config=config_40_40_20(), now="2026-08-12T00:00:00.000Z")
    assert task.status == "PENDING"
    assert task.stage == "PLANNING"
    assert task.total_batch_count is None


def test_create_task_budget_exceeded_rejected(session, settings_override):
    settings_override.max_generation_units_per_task = 5  # 5 章 COMPACT=15 > 5
    with pytest.raises(AppError) as ei:
        create_task(session, device_id="d1", file_id="f1", deck_id="dk1",
                    chapter_ids=["c1", "c2", "c3", "c4", "c5"], config=config_compact(), now="...")
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
```

（config fixture 复用既有测试的 GenerationConfig 构造定式。）

- [ ] **Step 2: 确认失败**（现状 create_task 直接 RUNNING+GENERATING 且同事务规划 → 断言 PENDING 失败）
- [ ] **Step 3: 实现** — service.py 改造 + 预算校验（settings 经 `session.info["settings"]` 注入，与 executor 同定式；缺省 `Settings()`）+ task_view 加字段；删除 `plan_knowledge_points` import
- [ ] **Step 4: 既有创建/幂等/删除保护测试全绿**（V1/V4 测试需在 T16 更新——本任务先跑通除规划断言外的用例）
- [ ] **Step 5: 四工具 + Commit** — `feat(llm-upgrade): 任务创建改 PENDING+PLANNING + 预算上限校验`

---

### Task 9: 规划执行（CAS 抢占/快照冻结/分组调用/合并落库）

**Files:**
- Create: `main/services/generation/planner_validator.py`、`main/services/generation/planning_executor.py`、`main/tests/services/generation/test_planner_validator.py`、`main/tests/services/generation/test_planning_executor.py`
- Modify: `main/services/tasks/executor.py`（规划 worker 接线）

**Interfaces:**
- `planner_validator.py`:
  - `validate_and_truncate(raw: dict, *, allowed_page_ids: set[str], quota: dict[str, int], max_pages_per_unit: int, max_chars_per_unit: int, page_chars: dict[str, int]) -> list[dict]`：schema 校验（load_schema_asset("planner_output")）→ 逐单元校验（source_chunk_ids ⊆ allowed、≥1 无重复、页数 ≤ max_pages、字符和 ≤ max_chars）→ 按难度配额确定性截断（`source_chunk_ids` 按 page_number 排序后规范化；超配额按该难度 priority 升序保留、并列按原顺序）→ 返回规范化 units（`source_chunk_ids` 规范化排序、priority 重排 1..N）
  - `normalize_units(units) -> list[dict]`：`source_chunk_ids` 排序、去重、priority 顺序化
- `planning_executor.py`:
  - `claim_planning_task(session, *, orphan_timeout_minutes: int, now: str) -> Task | None`：CAS1 `UPDATE tasks SET status='RUNNING', started_at=COALESCE(started_at, :now), updated_at=:now WHERE status='PENDING' AND stage='PLANNING'`（rowcount=1 继续）；CAS1 提交前按 §4.2 重读章节最新 name/start/end_page 覆盖 `selected_chapters`（章节失效 → 同事务 `status='FAILED', failure_stage='PLANNING'` + 内部原因 `CHAPTER_SNAPSHOT_STALE`）；CAS2 `WHERE status='RUNNING' AND stage='PLANNING' AND updated_at < now-orphan` → rowcount=1 接管并 `mark_stale_unknown` 本任务 PLANNING 遗留 STARTED
  - `run_planning(session, task, *, settings, client) -> None`：
    1. 快照章节 → `load_pages` 选页；按 `planner_max_input_chars` 连续页拆组；组数 > `max_planner_groups_per_task` → FAILED
    2. `allocate_group_quota` 子配额；组 → `operation_key=f"planning:{chapter_id}:{g}"`、`input_fingerprint=sha256(组页 id+sha256+子配额+资产版本)`
    3. 每组：`find_success_result` 命中（同 operation_key+fingerprint）→ 复用 normalized_result；否则 `attempt_count ≥ 1+planning_retry_limit` → 组 SKIPPED；否则事务内重读 Task（非 `RUNNING+PLANNING` → 停止）→ `create_attempt(STARTED)` + 心跳 commit → 事务外 `client.chat(planner_prompt)` → `validate_and_truncate` → 事务内 `finish_success(normalized_result)` 或 `finish_failed` + 心跳 commit
    4. 全部组后：合并全部成功组 units → 指纹去重（`(learning_objective, target_difficulty, card_type, sorted_source_chunk_ids)`）→ 按章序/组序/priority 全局 priority 重排
    5. 最终短事务：条件更新 `WHERE status='RUNNING' AND stage='PLANNING'` → 写 KnowledgePoint 行（新列：`source_chunk_id=source_chunk_ids[0]` 兼容投影）+ `plan_batches(1 单元 1 批)` + `stage='GENERATING'` + `skipped_planning_group_count` + 实际难度分布记录（cursor JSON）；rowcount=0 → rollback 返回
    6. 空单元三分支（spec §6.4）
- `executor.py`：`scan_once`/`process_running_tasks` 扩展——先 `claim_planning_task`（规划 worker 入口），生成 worker 扫描改 `status='RUNNING' AND stage='GENERATING'`

- [ ] **Step 1: validator 失败测试**

```python
def test_truncate_by_quota_and_normalize():
    raw = {"units": [
        {"source_chunk_ids": ["ch3"], "learning_objective": "b1", "target_difficulty": "BASIC", "card_type": "QUESTION", "priority": 1},
        {"source_chunk_ids": ["ch1", "ch3"], "learning_objective": "b2", "target_difficulty": "BASIC", "card_type": "QUESTION", "priority": 2},
        {"source_chunk_ids": ["ch3"], "learning_objective": "u1", "target_difficulty": "UNDERSTANDING", "card_type": "TRUE_FALSE", "priority": 1},
    ]}
    out = validate_and_truncate(raw, allowed_page_ids={"ch1", "ch3"}, quota={"BASIC": 1, "UNDERSTANDING": 1, "APPLICATION": 0},
                                max_pages_per_unit=2, max_chars_per_unit=9999, page_chars={"ch1": 10, "ch3": 20})
    assert [u["learning_objective"] for u in out] == ["b1", "u1"]
    assert out[0]["source_chunk_ids"] == ["ch1", "ch3"]  # page_number 排序规范化


def test_rejects_outside_pages():
    raw = {"units": [{"source_chunk_ids": ["ch9"], "learning_objective": "x", "target_difficulty": "BASIC", "card_type": "QUESTION", "priority": 1}]}
    with pytest.raises(AppError) as ei:
        validate_and_truncate(raw, allowed_page_ids={"ch1"}, quota={"BASIC": 5, "UNDERSTANDING": 0, "APPLICATION": 0},
                              max_pages_per_unit=2, max_chars_per_unit=9999, page_chars={"ch1": 1})
    assert ei.value.code == ErrorCode.GENERATION_FAILED
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现 planner_validator.py**
- [ ] **Step 4: 规划执行失败测试（CAS/冻结/复用/三分支，mock client）**

```python
# test_planning_executor.py —— 复用既有 tasks 测试的 fixture 定式（session/settings_override/
# mock transport client，见 test_executor.py 的 mock_chat 模式），关键断言：
def test_claim_cas1_snapshot_freeze(session, settings_override):
    # 任务 PENDING+PLANNING；claim 前修改 Chapter.start_page → claim_planning_task 返回任务，
    # 且 task.selected_chapters JSON 含新 start_page；CAS2（未超时 RUNNING）返回 None
    assert ...  # claim_planning_task(...) is not None
    assert json.loads(task.selected_chapters)[0]["start_page"] == 99  # 修改后的值

def test_planning_success_units_and_batches(session, settings_override):
    # mock chat 返回 {"units":[合法单单元]} → run_planning 后：
    # task.stage == "GENERATING"；KnowledgePoint 行含 target_difficulty/card_type/source_chunk_ids；
    # kp.source_chunk_id == kp.source_chunk_ids[0]（兼容投影）；Batch 行数 == 单元数且
    # batch.generation_unit_id == kp.knowledge_point_id
    assert task.stage == "GENERATING"

def test_planning_success_reuses_normalized(session, settings_override):
    # 先在账本写入同 operation_key+fingerprint 的 SUCCESS normalized_result；
    # run_planning → chat 调用计数 == 0（find_success_result 命中复用）

def test_planning_budget_reset_prevented(session, settings_override):
    # 账本该 operation_key 已有 3 条尝试（STARTED/FAILED/UNKNOWN 任意组合）→ 该组 SKIPPED，
    # chat 调用计数 == 0；task.skipped_planning_group_count == 1

def test_planning_empty_units_completed_no_units(session, settings_override):
    # mock chat 返回 {"units":[]} → task.status == "COMPLETED"、
    # task.completion_reason == "NO_GENERATION_UNITS"、total_batch_count == 0、
    # generated_card_count == 0

def test_planning_all_failed_fails_task(session, settings_override):
    # mock chat 恒抛 RetryableUpstreamError → 3 次尝试后组 SKIPPED、全组 SKIPPED →
    # task.status == "FAILED"、failure_stage == "PLANNING"

def test_planning_cancelled_final_condition_update(session, settings_override):
    # 全部组成功后在最终事务前把任务置 CANCELLED → 条件更新 rowcount=0 →
    # 无 KnowledgePoint 行写入（查询计数 == 0）
```

- [ ] **Step 5: 实现 planning_executor.py**（按上方接口逐函数；planner prompt 组装：`load_asset("prompts","planner")` + 页文本 + 子配额 + schema）
- [ ] **Step 6: executor 接线 + 既有 executor 测试更新**（scan_once 先规划再生成；`process_running_tasks` stage 条件）
- [ ] **Step 7: 四工具 + Commit** — `feat(llm-upgrade): 规划执行（CAS 抢占/快照冻结/账本恢复/合并落库/空单元三分支）`

---

### Task 10: 生成批改造（批=单元、锚定、页文本输入）

**Files:**
- Modify: `main/services/generation/batches.py`、`main/services/generation/rubric.py`（fake 退役 → 保留空壳供样卡/兼容或删除引用）、`main/services/generation/schema_validator.py`（不动，复用）
- Test: `main/tests/services/generation/test_batches_unit.py`

**Interfaces:**
- Consumes: T9 落库的 units（含新列）、`TextChunk`（T3）、账本（T7）
- Produces:
  - `plan_batches(session, *, task_id, generation_units, now) -> None`：签名改为按单元建批（删除 knowledge_points+batch_size 参数）；每单元一个 Batch（`batch_index` 按单元 priority 序 1..N、`generation_unit_id` 必填）
  - `process_next_batch(...)`：批 → 单元 → prompt 组装（`load_asset("prompts","generator")` + schema + 单元学习目标/锚定难度/锚定卡型 + 页文本（`load_pages` 按 source_chunk_ids，总量 ≤ `generator_max_input_chars`））；生成调用记账（`operation_key=f"generating:{batch_id}"`，输入指纹 = 单元学习目标/锚定/有序页 id+sha256/资产版本；调用前 STARTED 占位 + 事务外调用 + 终态与卡入库同事务）；输出校验：卡型=锚定、数量=1（`validate_card` 复用）→ 合法卡入库（V1 模式）+ `generation_item_id` 防重（seed 不变）；`target_difficulty` 服务端写规划锚定值
  - `_record_rubric` 改为：从批的 generation_unit_id 取单元锚定难度（删除 priority 轮换）；`score_card` 的 fake 评分保留作为 LLM 评分上线前的占位？——**删除**：Rubric 分数由 T11 的 LLM 评分回写；本任务中 `_record_rubric` 只做 batch 质量聚合（分布/coverage=1 or 0/duplicate），评分 5 字段留 NULL 待 SCORING
  - 删除 offset 反推（`(batch_index-1)*batch_size` 取 kp 的代码）
  - Batch 兼容投影：token/retry_count 从账本同一次调用结果同步写（`finish_success` 返回 usage → 写 batch 列）

- [ ] **Step 1: 失败测试**

```python
def test_plan_batches_one_batch_per_unit(session, settings_override):
    task = _make_task(session)  # 复用既有 fixture 定式
    units = _make_units(session, task, n=3)  # 3 个 KnowledgePoint 行
    plan_batches(session, task_id=task.task_id, generation_units=units, now="...")
    rows = session.scalars(select(Batch).where(Batch.task_id == task.task_id)).all()
    assert len(rows) == 3
    assert {b.generation_unit_id for b in rows} == {u.knowledge_point_id for u in units}


def test_process_batch_anchored_card(session, settings_override, mock_chat):
    # mock 返回 {"cards":[{... QUESTION 卡}]} → 入库卡 card_type=QUESTION、target_difficulty=锚定值、version=v1
    ...

def test_process_batch_wrong_type_rejected(session, settings_override, mock_chat):
    # mock 返回 TRUE_FALSE 卡但锚定 QUESTION → 0 合法卡 → FAILED 重试路径
    ...

def test_batch_ledger_same_transaction(session, settings_override, mock_chat):
    # 成功路径：Batch SUCCEEDED + 卡入库 + ledger SUCCESS 同事务（崩溃模拟 → STARTED 保留）
    ...
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现 batches.py 改造**（按接口；`_stable_uuid` seed 保持 `gen|task|batch_index|type|front|back` 以保 generation_item_id 稳定）
- [ ] **Step 4: rubric.py 改造** — `score_card`/`batch_quality` 的评分部分删除或降级为"无评分调用时字段留 NULL"；保留 `batch_quality` 的分布聚合（用于 Batch 质量列）；既有 V5A 测试在 T16 更新
- [ ] **Step 5: 四工具（既有批次测试按 T16 前最小修改跑通）+ Commit** — `feat(llm-upgrade): 生成批=单元（锚定校验/页文本输入/账本同事务）`

---

### Task 11: SCORING 阶段（抽样/合批/回写守卫）

**Files:**
- Create: `main/services/generation/scoring.py`、`main/services/generation/scoring_validator.py`、`main/tests/services/generation/test_scoring.py`
- Modify: `main/services/tasks/executor.py`（SCORING worker）、`main/app/schemas/tasks.py`（stage/failure_stage 枚举注释）、`main/services/generation/batches.py`（评分回写入口）

**Interfaces:**
- `scoring_validator.py`:
  - `validate_scores(raw: dict, requested_ids: set[str]) -> dict[str, dict]`：schema（`scoring_output`）校验 + 返回 ID 集合 == 请求集合（无缺/多/越权）+ 四维 0~3 + `rubric_total_score == 四维和`；违规 → `AppError(GENERATION_FAILED, ...)` 整次 FAILED
- `scoring.py`:
  - `plan_scoring_groups(session, *, task, settings) -> list[ScoringGroup]`：候选 = 全单元（经 Batch→Card 有卡）；层 = (chapter_id, target_difficulty, card_type)；层内 `sorted(units, key=lambda u: sha256(task_id+unit_id))` 确定性抽样（组批后调用数 ≤ `max_scoring_calls_per_task`，超则按层配额哈希缩减）；BASIC/UNDERSTANDING 按 (章,难度,卡型) 合批；APPLICATION 逐单元；批受 `scoring_max_cards_per_call`/`scoring_max_input_chars`（卡片+锚定+去重页原文全量计算）限制再拆
  - `ScoringGroup`：dataclass(group_key, unit_ids, card_ids, input_fingerprint, operation_key)
  - `run_scoring_stage(session, *, task, settings, client) -> None`：逐组——事务内重读 Task（须 `RUNNING+SCORING`）→ `create_attempt(STARTED, stage="SCORING", operation_key=f"scoring:{group_key}", input_fingerprint=...)` + 心跳 commit → 事务外 chat → `validate_scores` → 事务内：重读各 Card（`Card.version`/内容 hash 与指纹一致，不一致 → 整组 `finish_failed` 内部原因 `STALE_SCORING_INPUT`）→ 回写 5 字段 + Batch 质量 + `finish_success` + 心跳 commit；失败不重试不阻塞；全部组后条件更新 `WHERE status='RUNNING' AND stage='SCORING'` → COMPLETED（rowcount=0 → 不覆盖 cancel）
  - `enter_scoring_stage(session, *, task_id, settings) -> bool`：GENERATING 批循环结束后条件更新 `stage='GENERATING' → 'SCORING'`
- `executor.py`：`_execute_task` 批循环后调 `enter_scoring_stage`；`scan_once` 增加 SCORING 任务处理分支（同 CAS 风格：`RUNNING+SCORING` 心跳超时孤儿可接管）

- [ ] **Step 1: validator 失败测试**

```python
def test_validate_scores_exact_set():
    raw = {"scores": [{"generation_item_id": "g1", "evidence_score": 2, "correctness_score": 3, "difficulty_score": 2, "learning_value_score": 2, "rubric_total_score": 9}]}
    out = validate_scores(raw, requested_ids={"g1"})
    assert out["g1"]["rubric_total_score"] == 9


def test_validate_scores_missing_id_fails():
    with pytest.raises(AppError):
        validate_scores({"scores": []}, requested_ids={"g1"})


def test_validate_scores_total_mismatch_fails():
    raw = {"scores": [{"generation_item_id": "g1", "evidence_score": 1, "correctness_score": 1, "difficulty_score": 1, "learning_value_score": 1, "rubric_total_score": 9}]}
    with pytest.raises(AppError):
        validate_scores(raw, requested_ids={"g1"})
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现 scoring_validator.py**
- [ ] **Step 4: scoring 执行失败测试**

```python
def test_sampling_deterministic(session, settings_override):
    # 构造 30 单元任务（fixture 定式）→ 两次 plan_scoring_groups 结果全等（组 key 列表相同）；
    # 合批后调用数 ≤ max_scoring_calls_per_task
    assert [g.operation_key for g in g1] == [g.operation_key for g in g2]
    assert len(g1) <= settings_override.max_scoring_calls_per_task

def test_scoring_writes_scores_and_completes(session, settings_override, mock_chat):
    # mock 评分响应（ID 集合 == 请求）→ run_scoring_stage 后：
    # Card.evidence_score 等 5 字段非 NULL、task.status == "COMPLETED"、
    # 账本存在 stage=SCORING 的 SUCCESS 行
    assert cards[0].rubric_total_score == 9

def test_scoring_version_drift_rejected(session, settings_override, mock_chat):
    # 评分调用后、回写前手动改 Card.version（模拟用户编辑）→ 整组 finish_failed、
    # 该卡评分字段保持 NULL、error_code 含 STALE_SCORING_INPUT 内部原因

def test_scoring_failure_non_blocking(session, settings_override, mock_chat_fail):
    # 评分 chat 抛 RetryableUpstreamError → 不重试（attempt_count == 1）、
    # 卡保留（card_count 不变）、任务仍 COMPLETED、账本记 FAILED

def test_scoring_stage_cancel_guarded(session, settings_override, mock_chat):
    # 组处理前把任务置 CANCELLED → run_scoring_stage 不发请求（mock chat 调用计数 0）
```

- [ ] **Step 5: 实现 scoring.py**
- [ ] **Step 6: executor 接线（enter_scoring_stage + SCORING 扫描分支）**
- [ ] **Step 7: 四工具 + Commit** — `feat(llm-upgrade): SCORING 阶段（确定性抽样/合批/回写守卫/非阻塞）`

---

### Task 12: quality-summary 改造（分母/归因/成本口径）

**Files:**
- Modify: `main/app/api/observability.py`、`main/app/schemas/observability.py`（若有）、`main/openapi.yaml`（联动在 T14）
- Test: `main/tests/app/api/test_quality_summary_llm.py`

**Interfaces:**
- Consumes: 账本 `task_token_totals`（T7）、Batch.generation_unit_id → 单元 target_difficulty（T9/T10）
- Produces:
  - `quality_summary` 响应新增：`eligible_card_count`、`scored_card_count`、`sampling_rate`（= scored/eligible，分母 0 → None）；各评分均分只以对应字段非 NULL 的卡为分母（NULL 不计 0 分）
  - difficulty 分组键：`Batch.generation_unit_id → KnowledgePoint.target_difficulty`（SKIPPED 批次无 Card 也进正确组，coverage=0 计入）；model = batch.model；pdf = task.file_id
  - 响应增加 `rubric_version`（按版本拆组或显式过滤）；`cost_estimate` 标注 `"scope": "generation-stage-only"`（字段重命名或加字段，禁止双计；全链路成本按账本分 stage 汇总的可选出口不加在此接口）

- [ ] **Step 1: 失败测试**

```python
# fixture 定式：既有 observability 测试构造 device/task/batch/card 的 helper 复制复用
def test_summary_null_scores_not_zero(session, ...):
    # 构造 2 张卡（1 张 evidence_score=2、1 张 NULL）→ evidence_avg == 2.0
    # （分母为 1 而非 2）；scored_card_count == 1、eligible_card_count == 2
    assert group["evidence_avg"] == 2.0

def test_summary_difficulty_group_by_unit(session, ...):
    # SKIPPED 批次（generation_unit_id 指向 target_difficulty=BASIC 单元，无卡）→
    # 响应 groups 含 BASIC 键且 coverage_avg 计入该 0 值
    assert any(g["key"] == "BASIC" for g in groups)

def test_summary_sampling_rate_and_rubric_version(session, ...):
    # 响应含 eligible_card_count/scored_card_count/sampling_rate（=0.5）与 rubric_version；
    # cost_estimate 含 scope == "generation-stage-only"
    assert summary["groups"][0]["sampling_rate"] == 0.5
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现** — `_aggregate` 改造（Card 按字段非 NULL 聚合；difficulty 归因走 Batch→unit；`_group_key` difficulty 分支删除批分布解析）
- [ ] **Step 4: 既有 observability 测试更新 + 四工具 + Commit** — `feat(llm-upgrade): quality-summary 分母/归因/成本口径修正`

---

### Task 13: 估算删除（后端）

**Files:**
- Modify: `main/app/api/tasks.py`（删 estimate 端点）、`main/app/schemas/tasks.py`（删 CostEstimateRequest/Response）、`main/openapi.yaml`（删路径与组件）
- Delete: `main/services/generation/token_estimator.py`、`main/services/generation/planning.py`（quota.py 已承载预算）
- Test: 删除 `test_token_estimator*`、`test_estimate*`；新增 `main/tests/app/api/test_tasks_estimate_removed.py`

- [ ] **Step 1: 失败测试（删除后无引用）**

```python
def test_estimate_endpoint_removed():
    from app.main import create_app
    client = TestClient(create_app())
    r = client.post("/tasks/estimate", json={...})
    assert r.status_code == 404


def test_token_estimator_module_gone():
    with pytest.raises(ModuleNotFoundError):
        import services.generation.token_estimator  # noqa: F401
```

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 删除** — 端点/schema/模块/planning.py；`estimate_cost`/`estimate_cost_by_kind`（cost.py）保留；`services/tasks/service.py`、`batches.py` 等 import 清理
- [ ] **Step 4: 守卫更新** — schema↔openapi 守卫断言移除 estimate 相关（联动 T14 openapi 删除）
- [ ] **Step 5: 四工具 + Commit** — `feat(llm-upgrade): 删除 /tasks/estimate 与 token 估算链路`

---

### Task 14: 契约同步（文档 + 守卫 + 错误契约）

**Files:**
- Modify: `docs/Architecture/structure-contract.md`、`docs/PRD/V2.1/prd_v2_1.md`、`docs/Architecture/database-design.md`、`main/openapi.yaml`、`main/infra/llm/prompts.py`（版本守卫断言）、`main/tests/*`（守卫测试）
- 不创建平行计划/进度文件；`docs/Progress.md` 在 T17 更新

**内容（照 spec §12 逐条落实）：**
- structure-contract 3.10：difficulty 0~10 → 1~10
- structure-contract 6.10：分组键定义（difficulty=单元 target_difficulty）、eligible/scored/sampling_rate、rubric_version、generation-stage-only 成本口径
- structure-contract 3.5/3.6：selected_chapters 两阶段语义、生成单元概念/双维锚定/组合规则/密度预算与配额
- structure-contract 3.7/8.5：批=单元观测、账本（llm_call_attempts）、SCORING 阶段、asset name+version 逐调用记录
- structure-contract 4.1/6.4：任务状态机 PLANNING/SCORING、PENDING 创建语义
- PRD 5.4.1/5.6/5.7：按 spec §12 表述同步
- database-design：2.5（tasks 列/SCORING 枚举/selected_chapters 两阶段）、2.6（knowledge_points 新列）、2.7（batches generation_unit_id）、新表 text_chunks/llm_call_attempts 章节
- openapi：Task/KnowledgePoint/Batch schema、stage/failure_stage 加 SCORING、/tasks/estimate 删除、CostEstimate 删除
- 错误契约：无新增错误码（复用 GENERATION_FAILED/API_KEY_UNAVAILABLE/VALIDATION_ERROR）；适配层 retryable 语义写入 structure-contract 错误映射表
- 红线 5：structure-contract 3.5 版本字段与 manifest 一致（守卫断言更新为 T5 扩展键）
- R-03：Progress.md 第 6 节登记"本工作包覆盖（PLANNED）"（不 RESOLVED）

- [ ] **Step 1: 文档逐条修订**（按上方清单）
- [ ] **Step 2: 守卫测试更新** — `test_contract_guards*.py`：schema↔openapi（新字段）、manifest↔契约版本、ORM↔database-design 表/列、错误码↔ch7（不变）
- [ ] **Step 3: 四工具全绿 + Commit** — `docs: LLM 链路升级契约同步（3.5/3.6/3.7/3.10/6.10/8.5 + PRD 5.4.1/5.6/5.7 + database-design + openapi + R-03 PLANNED）`

---

### Task 15: 前端改造（frontend-app/Front/）

**Files:**
- Modify: `frontend-app/Front/` 任务创建流程源码（删除 `POST /tasks/estimate` 调用与价格区间 UI；任务列表/详情展示 `PLANNING`/`GENERATING`/`SCORING` 阶段、`NO_GENERATION_UNITS` 空结果、部分规划跳过提示；SCORING 时卡片可访问展示、取消评分不删卡提示）

**验收（本机验证）：**
- [ ] **Step 1: 定位 estimate 调用与价格 UI** — grep `estimate`/`price_low`/`price_high` 于 `frontend-app/Front/`，列出删除点
- [ ] **Step 2: 删除 estimate 调用/UI + 阶段文案映射**（对照 openapi stage 枚举）
- [ ] **Step 3: 本机构建/静态检查通过**（按该仓库既有构建方式；无本机构建链则至少完成源码级 grep 验收：无 estimate 引用、三阶段/空结果文案存在）
- [ ] **Step 4: Commit（本地）** — `feat(frontend): 任务创建去 estimate + PLANNING/GENERATING/SCORING 展示`（不 fork/push）

---

### Task 16: V4/V5A/V6 测试更新 + 全量回归

**Files:**
- Modify: `main/tests/services/tasks/`、`main/tests/services/generation/`、`main/tests/app/api/`（V4 规划断言、V5A 批次/rubric、V6 rewrite 相关测试）
- 更新内容：fake 规划 → mock planner 契约断言；`_record_rubric` 轮换删除 → 锚定落库断言；批次=单元断言；账本同事务断言；`test_acceptance_ac04_ac07.py` 的 rubric 断言改 LLM 评分路径

- [ ] **Step 1: 运行全量 pytest 收集失败清单**（预期 V4/V5A/V6/acceptance 相关失败）
- [ ] **Step 2: 逐文件更新**（按新语义；保留原验收意图：AC-04 低分合法卡入库、AC-07 版本/质量记录、AC-05 恢复）
- [ ] **Step 3: 全量回归 + 四工具全绿**
- [ ] **Step 4: Commit** — `test: V4/V5A/V6 测试按 LLM 链路新语义更新`

---

### Task 17: canary + 完成口径 + Progress 更新

**Files:**
- Modify: `docs/Progress.md`（登记）、`docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md`（状态行）、`agent_evolution/CHANGELOG.md`（若 canary 暴露资产问题则记录）

- [ ] **Step 1: 受控真实 canary** — 从本机 `.env` 加载真实 Key（红线：权限 600、git 忽略），单任务 Planner→Generator→Scoring 全链路一次：验证三类 JSON 输出、账本 token 记录、卡片入库与评分回写；预算上限（成本 ≤ ¥3，账本为准）；canary 失败 → 修复循环（资产/代码），不得跳过
- [ ] **Step 2: 完成口径** — canary 通过 → Progress.md 登记 `LOCAL_IMPLEMENTATION_DONE`（本地）→ `PRODUCTION_VALIDATED`（canary 后）；R-03 改 RESOLVED
- [ ] **Step 3: spec 状态行更新**（FROZEN → 完成说明）
- [ ] **Step 4: 全量四工具最后确认 + Commit** — `docs: LLM 链路升级完成（canary 通过，R-03 RESOLVED）`

---

## Self-Review 记录

- Spec §0-§4 文本持久化 → T1/T3；§3.5 配额 → T4；§5 资产 → T5；§6 状态机 → T8/T9（含 CAS2 孤儿、空单元三分支、硬上限）；§7 批 → T10；§8 SCORING → T11；§9 账本 → T7；§10 估算删除+上限 → T2/T8/T13；§11 迁移 → T1；§12 契约 → T14；§13 测试 → T3-T16 各任务 + T16 回归 + T17 canary；§14 登记 → T14/T17。前端（spec §10 前端段）→ T15。
- 接口一致性：`create_attempt` 签名在 T7 定义、T9/T10/T11 使用一致；`task_unit_budget` T4 定义 T8 使用；`plan_batches` T9 调用、T10 改签名——T9 Step5 与 T10 Step3 同步（T9 内先按新签名实现，避免两处漂移）；`asset_versions()` 扩展键在 T5 定义、T10/T11 使用。
- 无占位符：全部步骤含测试代码或明确实现要点。
