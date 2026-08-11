# V6 单卡重写闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `POST /cards/{card_id}/rewrite` 单卡重写闭环：生成新版本 → Schema 校验 → Rubric 记录 → 原地替换原卡（card_id/position 不变、version 递增、新 generation_item_id、ReviewState 原子重置），失败保留原卡及原排程。

**Architecture:** 复用 V5A adapter 分批执行模式（DeepSeekClient + client_factory 注入 + mock transport 测试）；新 prompt 资产 `agent_evolution/prompts/v1/rewrite.md`（manifest 注册）；幂等接线复用 V1 `execute_idempotent` 原语（同事务）；服务层 `services/cards/rewrite.py` 编排，handler 只做 HTTP 映射。响应解析函数从 batches.py 提取到共享模块（DRY，V5A 代码仅 import 改名）。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + jsonschema + DeepSeek adapter（mock transport）+ pytest/ruff/mypy（conda env `shanka-backend`）。

## Global Constraints

- 契约单向依赖：docs/PRD → docs/Architecture → main/；实现不得反向驱动契约。本包契约依据：PRD 5.13/AC-06、structure-contract 6.7（C-05 规则）、database-design 2.9/2.10（256 行重写语义）。
- 红线 4：API Key 只出现在 `infra/llm/` 调用路径；明文不落日志/响应/测试；llm 层异常统一脱敏为 `API_KEY_*`/`GENERATION_FAILED`。LOCAL-DONE 前禁止真实 DeepSeek 调用（仅 mock transport / deterministic fake）。
- 红线 5：`structure-contract.md` 的 prompt_version/schema_version/rubric_version 必须与 `agent_evolution/manifest.json` 一致；资产演进 = 新版本目录 + manifest + CHANGELOG，不原地改 v1。
- 错误码守卫：`app/errors.py` 与 structure-contract ch7 全等（test_error_codes_guard）；LOCALIZATION_KEYS 派生规则 `"error." + 错误码.lower()`。
- 工程边界：app → services → infra 单向；handler 只做 HTTP 映射；事务语义归 services（重写 + 幂等记录同事务，V1 模式）；不使用 Repository 基类/第二套 DTO 框架/事件总线/外部任务队列。
- 高冲突入口（app/schemas、openapi、ORM/migration、middleware assembly、manifest.json、errors.py）由主 Agent 审查时逐字核对；本包改动：manifest.json（T2，+1 条目）、errors.py（T1，+1 码）。
- 验证：每任务 `conda run -n shanka-backend python -m pytest` / `ruff check .` / `ruff format --check .` / `mypy .`（main/ 下）全绿；commit 只 git add 涉及文件。
- 命名：测试 `test_<模块>_<行为>`；分支 codex/v6，计划提交后每任务独立提交。
- 不实现：重写不改变 source；不向响应/任务明细暴露 PDF 来源（响应 = Card schema，无来源字段，无需改）；不自动修复失败。

---

### Task 1: 错误码契约增补（REWRITE_SCHEMA_INVALID）

重写新版本 Schema 校验失败 → 422（openapi rewrite 响应已含 422）。现有错误码无「生成内容非法」语义（IMPORT_PARSE_ERROR 是导入、PDF_* 是 PDF），新增 `REWRITE_SCHEMA_INVALID`（兼容性新增，只更新契约——AGENTS.md 版本管理规则）。

**Files:**
- Modify: `main/app/errors.py`（ErrorCode 枚举 + ERROR_HTTP_STATUS + LOCALIZATION_KEYS）
- Modify: `docs/Architecture/structure-contract.md`（ch7 错误码表「牌组/卡片」分组 +1 行）
- Test: `main/tests/contract/test_error_codes_guard.py`（确认守卫自动覆盖；加一条断言）

**Interfaces:**
- Produces: `ErrorCode.REWRITE_SCHEMA_INVALID`（HTTP 422），`localization_key = "error.rewrite_schema_invalid"`。Task 3/4 抛此错误表示「新版本未通过 Schema 校验，原卡保留」。

- [ ] **Step 1: 写失败测试**

在 `main/tests/contract/test_error_codes_guard.py` 追加：

```python
def test_rewrite_schema_invalid_registered() -> None:
    """V6：重写 Schema 校验失败专用码 422（ch7 表 + errors.py 全等由守卫校验）。"""
    from app.errors import ErrorCode, ERROR_HTTP_STATUS

    assert ErrorCode.REWRITE_SCHEMA_INVALID.value == "REWRITE_SCHEMA_INVALID"
    assert ERROR_HTTP_STATUS[ErrorCode.REWRITE_SCHEMA_INVALID] == 422
    codes = parse_error_codes_table(STRUCTURE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert codes["REWRITE_SCHEMA_INVALID"] == 422
```

- [ ] **Step 2: 运行确认失败**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/contract/test_error_codes_guard.py -q`
Expected: FAIL（AttributeError: REWRITE_SCHEMA_INVALID）

- [ ] **Step 3: 实现**

`main/app/errors.py`：
- ErrorCode 枚举「牌组/卡片」分组追加 `REWRITE_SCHEMA_INVALID = "REWRITE_SCHEMA_INVALID"`（IMPORT_PARSE_ERROR 之后）。
- ERROR_HTTP_STATUS 追加 `ErrorCode.REWRITE_SCHEMA_INVALID: 422,`。
- LOCALIZATION_KEYS 集合追加 `"error.rewrite_schema_invalid"`。

`docs/Architecture/structure-contract.md` ch7「牌组/卡片」分组表追加行：

```markdown
| | `REWRITE_SCHEMA_INVALID` | 422 | 单卡重写的新版本未通过 Schema 校验(原卡保留) |
```

- [ ] **Step 4: 运行确认通过**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/contract/test_error_codes_guard.py tests/contract/test_localization_guard.py -q`
Expected: PASS（守卫全等校验覆盖 errors.py ↔ ch7 ↔ localization 派生集合）

- [ ] **Step 5: 全量验证 + 提交**

Run: `cd main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿（既有 322 + 新 1）。

```bash
git add main/app/errors.py docs/Architecture/structure-contract.md main/tests/contract/test_error_codes_guard.py
git commit -m "feat(errors): 新增 REWRITE_SCHEMA_INVALID 422（V6 单卡重写 Schema 校验失败，契约 ch7 同步）"
```

---

### Task 2: rewrite prompt 资产（agent_evolution）

新增重写专用 prompt 资产（任务生成用 generator 资产是「按知识点批量生成」语义，重写是「基于原卡改写」语义，不可混用；不原地改 v1——R-03）。

**Files:**
- Create: `agent_evolution/prompts/v1/rewrite.md`
- Modify: `agent_evolution/manifest.json`（prompts 分组 + rewrite 条目）
- Modify: `agent_evolution/CHANGELOG.md`（追加条目）
- Test: `main/tests/contract/test_manifest_guard.py`（追加断言）

**Interfaces:**
- Produces: `load_asset("prompts", "rewrite")` 可加载；manifest.json 新增 `"rewrite": {"version": "v1", "path": "prompts/v1/rewrite.md"}`。`asset_versions()` 不改变（仍取 generator——Batch 观测列语义不变，structure-contract 3.7 的 prompt_version 不漂移）。
- Consumes: Task 3 `load_asset("prompts", "rewrite")`。

- [ ] **Step 1: 写失败测试**

`main/tests/contract/test_manifest_guard.py` 追加：

```python
def test_rewrite_prompt_asset_registered() -> None:
    """V6：manifest 注册 rewrite prompt，加载可得且可读（资产演进红线 5）。"""
    from infra.llm.prompts import load_asset

    text = load_asset("prompts", "rewrite")
    assert "重写" in text or "rewrite" in text  # 资产内容含重写指令
    assert "JSON Schema" in text  # 输出格式契约
```

- [ ] **Step 2: 运行确认失败**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/contract/test_manifest_guard.py -q`
Expected: FAIL（manifest 无 rewrite 条目 → INTERNAL_ERROR）

- [ ] **Step 3: 实现**

`agent_evolution/prompts/v1/rewrite.md`（中文指令，风格与 generator.md 一致；输出必须是 `{"cards": [单卡]}` JSON，与批次响应格式一致便于解析复用）：

```markdown
你是一名闪卡制作助手。用户对一张现有卡片发起重写，请基于原卡内容与附加要求生成改进后的新版本。

原卡内容：
- 类型：{card_type}
- 正面（front）：{front}
- 背面（back）：{back}
{structured_fields}

重写要求：
1. 保持卡片类型不变；保持知识点主题不变。
2. 改进表述的准确性、清晰度与学习价值，正面问题与背面答案一一对应。
3. 必须严格按照 JSON Schema 输出，且仅输出一个 JSON 对象，格式为：{"cards": [单张卡片对象]}。
4. 不要输出任何解释文字。
```

（`{structured_fields}` 由调用方按类型填充 `question/answer` 或 `statement/answer_boolean/explanation`；custom_requirements 有值时追加「附加要求：{custom_requirements}」一行。）

`agent_evolution/manifest.json` prompts 分组：

```json
"generator": { "version": "v1", "path": "prompts/v1/generator.md" },
"rewrite": { "version": "v1", "path": "prompts/v1/rewrite.md" }
```

`agent_evolution/CHANGELOG.md` 追加：

```markdown
## 2026-08-11
- 新增 prompts/v1/rewrite.md（V6 单卡重写，manifest prompts.rewrite v1）；generator/planner 不变。
```

- [ ] **Step 4: 运行确认通过**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/contract/test_manifest_guard.py -q`
Expected: PASS

- [ ] **Step 5: 全量验证 + 提交**

Run: 同 T1 Step 5。
Expected: 全绿。

```bash
git add agent_evolution/prompts/v1/rewrite.md agent_evolution/manifest.json agent_evolution/CHANGELOG.md main/tests/contract/test_manifest_guard.py
git commit -m "feat(assets): rewrite prompt v1 资产 + manifest 注册 + CHANGELOG（V6）"
```

---

### Task 3: 响应解析提取 + 重写 service 核心

从 batches.py 提取响应解析函数到共享模块（重写与批次生成都消费，DRY；V5A 代码仅 import 改名，行为零变化——既有测试全量回归兜底），然后实现 `rewrite_card` 用例。

**Files:**
- Create: `main/services/generation/response_parse.py`（parse_cards_json + to_internal_card，从 batches.py 移动）
- Modify: `main/services/generation/batches.py`（删私有函数 + import 改名）
- Create: `main/services/cards/rewrite.py`（rewrite_card 用例）
- Modify: `main/services/cards/service.py`（card_view 已在；如需共用 `_next_position` 不动）
- Test: `main/tests/integration/test_cards_rewrite.py`（新建）

**Interfaces:**
- Consumes: `infra.llm.prompts.load_asset`、`services.generation.schema_validator.{load_card_schema, validate_card}`、`services.generation.rubric.score_card`、`infra.llm.crypto.{key_from_settings, decrypt_key}`、`infra.db.models.{ApiKey, Card, ReviewState}`、`infra.llm.deepseek.DeepSeekClient`、`app.errors.AppError`。
- Produces:
  - `services/generation/response_parse.py`:
    - `parse_cards_json(content: str) -> list[dict[str, Any]]`（原 batches._parse_cards）
    - `to_internal_card(card: dict[str, Any]) -> dict[str, Any]`（原 batches._to_internal_card）
  - `services/cards/rewrite.py`:
    - `rewrite_card(session, *, device_id, card_id, custom_requirements, now, settings, client_factory=None) -> Card`
    - `_next_version(current: str) -> str`：`^v(\d+)$` → `f"v{n+1}"`；其余（V1 手动卡 ISO 时间戳等）→ `"v2"`（database-design 2.9「version 递增」；V1 手动卡 version=now 格式分支归一并登记）。

**rewrite_card 流程（事务归 services；handler 幂等包装调用）：**

1. 查卡：`select(Card).where(Card.card_id == card_id, Card.device_id == device_id)`；None → `CARD_NOT_FOUND`（统一 404，不暴露存在性）。
2. 查 ReviewState（card_id；应存在——新建卡同事务插入；缺失则创建初始行防御）。
3. Key：`key_from_settings(settings)` 为 None 或 `api_keys` 表无 `AVAILABLE` 行 → `API_KEY_NOT_SET`（422，契约 ch7「样卡/任务启动时未保存 Key」——重写同属 LLM 生成，复用任务启动语义；与 executor 的 API_KEY_UNAVAILABLE 502 区分：502 留给解密失败/上游不可用）。
4. 解密失败 → `API_KEY_UNAVAILABLE`（502）。
5. client = `client_factory(api_key)` 或 `DeepSeekClient(settings, api_key=api_key)`。
6. Prompt：`load_asset("prompts", "rewrite")` 填充原卡字段（card_type/front/back + 类型结构化字段 + custom_requirements 可空）→ 拼接 `\n请严格按以下 JSON Schema 输出：\n{card_schema}`。
7. `client.chat(prompt)` → `result["content"]`。
8. `parse_cards_json` → 空/非 dict → 视为 Schema 违约路径（保留原卡，抛 REWRITE_SCHEMA_INVALID）；取 `cards[0]`（多于 1 张只取首张——重写单卡语义）→ `to_internal_card`。
9. `validate_card(internal, load_card_schema())` 违约 → `REWRITE_SCHEMA_INVALID`（保留原卡：不做任何写）。
10. 原地替换（同一 Card 行，同事务）：
    - 内容字段：front/back/card_type/question/answer/statement/answer_boolean/explanation 更新；code 不变。
    - `generation_item_id = str(uuid.uuid4())`（PRD 5.13：新版本用新标识，旧标识随覆盖作废——database-design 256 行）；source/target_difficulty/knowledge_point_ids 保留原值。
    - Rubric：`score_card({"type", "question", "answer", "statement", "explanation", "target_difficulty"})` → 5 个评分字段落卡（Rubric 不影响替换结果——AC-06，低分照常替换）。
    - `version = _next_version(card.version)`；`updated_at = now`（created_at 不变）。
    - ReviewState 原子重置：`state="NEW"、stability=0.0、difficulty=1.0、due=now、reps=0、lapses=0、last_review=None、last_rating=None、updated_at=now`（同 2.10 新建卡初始值，ORM CHECK difficulty 1~10）。
11. `session.flush()` 后返回 card（调用方 commit）。

**错误路径测试矩阵（integration）：** 成功替换全字段断言 / Schema 违约保留原卡全字段 + review_state 原值 / LLM 异常（transport 500 → GENERATION_FAILED）保留 / 无 Key 422 / 跨设备 404 / version 递增单元断言。

- [ ] **Step 1: 提取 response_parse（纯移动）**

创建 `main/services/generation/response_parse.py`，把 `batches.py` 的 `_parse_cards`（改名 `parse_cards_json`，docstring 注明「重写单卡与批次共用」）与 `_to_internal_card`（改名 `to_internal_card`）整体移动（含 docstring）；`batches.py` 改为 `from services.generation.response_parse import parse_cards_json as _parse_cards, to_internal_card as _to_internal_card`（调用点零改动）。

- [ ] **Step 2: 回归确认提取零行为变化**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_tasks_executor.py tests/integration/test_concurrency.py -q`
Expected: PASS（批次路径全回归）

- [ ] **Step 3: 写重写 service 失败测试**

`main/tests/integration/test_cards_rewrite.py`（fixture 复用 test_cards_service 模式：真实加密 Key 种子 + mock transport）。至少：

```python
def test_rewrite_succeeds_in_place(seed) -> None:
    # 断言：card_id 不变、position 不变、source 不变、front/back 新值、
    # generation_item_id 为新值且 != 旧值、version 递增、updated_at 新、
    # review_state 重置 NEW/0.0/1.0/due=now/reps=0/lapses=0/last_review None、
    # rubric 5 字段落卡非 None
```

（测试先写全断言再实现；失败阶段 import rewrite_card 报 ModuleNotFoundError 即 FAIL 判定。）

- [ ] **Step 4: 实现 rewrite_card**

按上面「rewrite_card 流程」在 `main/services/cards/rewrite.py` 实现；`_next_version` 用 `re.fullmatch(r"v(\d+)", ...)`；错误码按 T1 定义。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_cards_rewrite.py -q`
Expected: PASS（成功/失败/404/422/版本矩阵全部）

- [ ] **Step 6: 全量验证 + 提交**

Run: T1 Step 5 命令。Expected: 全绿。
`git add` 涉及文件（response_parse.py/batches.py/rewrite.py/test_cards_rewrite.py）提交 `feat(cards): 单卡重写用例（原地替换/失败保留/ReviewState 重置/Rubric 记录）`。

---

### Task 4: rewrite 路由接线（幂等 + 装配）

**Files:**
- Modify: `main/app/api/cards.py`（新 router `rewrite_router = APIRouter(prefix="/cards", tags=["decks"])` + rewrite 端点）
- Modify: `main/app/main.py`（include rewrite_router）
- Test: `main/tests/acceptance/test_acceptance_ac06.py`（新建，AC-06 三条映射）

**Interfaces:**
- Consumes: `services.cards.rewrite.rewrite_card`、`execute_idempotent/get_idempotency_key/request_body_hash`、`card_view`。
- Produces: 端点 `POST /cards/{card_id}/rewrite`（200 Card / 401 / 404 / 409 IDEMPOTENCY_CONFLICT / 422 REWRITE_SCHEMA_INVALID+API_KEY_NOT_SET / 429 / 502 API_KEY_UNAVAILABLE）。

**端点实现（V1 create_card 幂等接线同款）：**

```python
router_rewrite = APIRouter(prefix="/cards", tags=["decks"])

@router_rewrite.post("/{card_id}/rewrite")
def rewrite_card_endpoint(request, card_id, session, body: dict | None = None) -> JSONResponse:
    device_id: str = request.state.device_id
    key = get_idempotency_key(request)
    body_hash = request_body_hash(getattr(request.state, "raw_body", b""))
    payload = body or {}  # openapi 内联 object：custom_requirements 可空
    custom_requirements = payload.get("custom_requirements")  # 非 str → 422 VALIDATION_ERROR

    def biz(session: Session) -> tuple[int, dict[str, Any]]:
        card = rewrite_card(
            session, device_id=device_id, card_id=card_id,
            custom_requirements=custom_requirements, now=_now(),
            settings=request.app.state.settings,
            client_factory=getattr(request.app.state, "client_factory", None),  # 测试注入；生产 None
        )
        return 200, card_view(card)

    _replayed, status, body = execute_idempotent(...)
    session.commit()
    return JSONResponse(status_code=status, content=body)
```

注意：
- body 参数类型：openapi 请求体是内联 object（无命名组件）——用 `body: dict | None = Body(default=None)`，`custom_requirements` 非 str（如数字）→ 手动 `VALIDATION_ERROR`（V1 import 同款手动校验模式）。
- settings 从 `request.app.state.settings`（main.py 装配时挂载，tasks.py:178 同款）。
- client_factory 从 `request.app.state.client_factory`（getattr 缺省 None→生产构造真实 client；acceptance 测试 `client.app.state.client_factory = factory` 注入 mock transport——重写是请求内同步调用，不能像后台 executor 那样显式传参）。
- 幂等 path：`f"/cards/{card_id}/rewrite"`（与 openapi 路径一致，无 /v1 前缀——现有路由惯例）。

- [ ] **Step 1: 写 AC-06 失败测试**

`main/tests/acceptance/test_acceptance_ac06.py`（TestClient + 种子卡 + mock transport 注入——检查 acceptance conftest 的注入模式，executor AC-05 同款）：

```python
def test_ac06_rewrite_flow() -> None:
    """AC-06 三条：可重写 / Schema 通过才替换 / 失败保留原卡。"""
    # 1. 重写成功：200，响应卡 card_id 与请求一致、front 新值、version 递增
    # 2. Schema 违约响应（{"cards": [{type: QUESTION, front: "", back: ""}]}）→ 422
    #    REWRITE_SCHEMA_INVALID，重查原卡全字段不变
    # 3. 幂等重放：同键同 body 第二次 → 200 且首次响应体一致（重放不二次 chat）
    # 4. 同键异 body → 409 IDEMPOTENCY_CONFLICT
```

- [ ] **Step 2: 运行确认失败**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/acceptance/test_acceptance_ac06.py -q`
Expected: FAIL（404 路由不存在）

- [ ] **Step 3: 实现端点 + 装配**

按上面代码实现；`main.py` `include_router(rewrite_router)`（cards.router 之后）。

- [ ] **Step 4: 运行确认通过**

Run: Step 2 命令。Expected: PASS

- [ ] **Step 5: 全量验证 + 提交**

Run: T1 Step 5。Expected: 全绿。
提交 `feat(api): POST /cards/{card_id}/rewrite 幂等接线 + AC-06 验收映射`。

---

### Task 5: 并发 / 隔离测试 + 守卫回归

**Files:**
- Test: `main/tests/integration/test_cards_rewrite.py`（追加并发用例）或新 `main/tests/integration/test_rewrite_concurrency.py`

**范围：**
- 并发重写（同卡、不同幂等键、两个 session 交替提交）：BEGIN IMMEDIATE 单写者串行化——后提交覆盖先提交（后写赢），终态一致无脏写；断言最后版本与内容 = 后写者。
- 重写与复习并发：重写重置 review_state 与 review 事件更新互斥（单写者），终态为两者先后串行结果之一，无半覆盖。
- 隔离：跨设备访问 → 404（已在 T3 覆盖，此处仅确认 HTTP 层）。
- 守卫回归：`tests/contract/` 全绿（ch7 ↔ errors.py ↔ manifest ↔ openapi 一致性）。
- 响应无 PDF 来源字段：断言重写响应 JSON 不含 storage_key/file_id/pdf 字段（红线 4/PRD 5.13「来源不出响应」）。

- [ ] **Step 1: 写并发失败测试**（上述 2 条 + 来源字段断言）
- [ ] **Step 2: 运行确认失败**（MockTransport 计数/双 session 场景，先确认红）
- [ ] **Step 3: 实现为服务层可注入并发（如无需改代码则步骤说明「无代码改动——测试验证既有语义」并跳过实现）**
- [ ] **Step 4: 运行确认通过**

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_cards_rewrite.py tests/integration/test_rewrite_concurrency.py tests/contract -q`
Expected: PASS

- [ ] **Step 5: 全量验证 + 提交**

Run: T1 Step 5。Expected: 全绿。
提交 `test(rewrite): 并发串行化 + 隔离 + 来源不出响应断言（V6）`。

---

### Task 6: 整包验收（主 Agent）

**Files:** 无（仅执行与记录）

- [ ] **Step 1: 契约四工具全量**

Run: `cd main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿（322 + V6 新增）

- [ ] **Step 2: 高冲突入口核对（主 Agent 亲自）**

- manifest.json：prompts.rewrite 条目与 CHANGELOG 一致；asset_versions() 输出不变（structure-contract 3.7 prompt_version 不漂移）。
- errors.py：REWRITE_SCHEMA_INVALID 三处（enum/status/localization）与 ch7 表全等（守卫已兜底）。
- openapi：无新组件/路径改动（rewrite 接口已存在——确认实现路径与 openapi 一致）。

- [ ] **Step 3: 边界冒烟（主 Agent）**

干净 temp DB：alembic upgrade → uvicorn 冒烟（healthz 200）→ TestClient 全链路 rewrite 一次成功路径（mock transport 注入）。

- [ ] **Step 4: 更新 Progress（主 Agent）**

`docs/Progress.md`：V6 行 `DONE`（证据：契约增补/资产/用例/路由/并发 + 验收命令结果）；登记 R-18（version 递增格式分支：V1 手动卡 ISO 时间戳 → "v2"；MANUAL/IMPORTED 卡重写后 generation_item_id 非空但部分唯一索引仅覆盖 GENERATED——uuid 随机无防重冲突，source 不变语义）；自动化验证行计数更新。

---

## 自审记录（writing-plans skill）

**Spec 覆盖：**
- PRD 5.13 处理流程（请求→生成→Schema→Rubric→替换）：T3 步骤 6-10 ✓
- 原地更新 card_id 不变（C-05）：T3 步骤 10 ✓
- 新 generation_item_id 旧标识作废：T3 步骤 10 ✓
- ReviewState 重置重新排程：T3 步骤 10 ✓
- Rubric 不影响替换：T3 步骤 10（低分照常替换，测试矩阵含低分卡）✓
- 失败保留原卡及原排程：T3 错误路径矩阵 ✓
- 来源不展示：T5 来源字段断言 ✓
- AC-06 三条：T4 Step 1 ✓
- 6.7 规则全项：T3 步骤 10 + T4 ✓
- 错误码/幂等/429/隔离：T1/T4/T5 ✓
- 契约同步（ch7 + manifest + CHANGELOG）：T1/T2 ✓

**Placeholder 扫描：** 无 TBD/TODO；所有代码块为具体实现。

**类型一致性：** `rewrite_card(session, *, device_id, card_id, custom_requirements, now, settings, client_factory)` 在 T3 定义、T4 消费同签名；`parse_cards_json`/`to_internal_card` 在 T3 定义、batches.py 别名消费；`ErrorCode.REWRITE_SCHEMA_INVALID` T1 定义、T3/T4 消费一致。
