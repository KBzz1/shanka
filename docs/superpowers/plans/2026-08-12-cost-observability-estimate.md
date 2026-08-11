# Agent 成本观测能力层 + 任务价格预估接口实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可演进的 token 用量估算模型(Agent 成本观测能力层),并落地第一个消费点 `POST /v1/tasks/estimate` 创建任务前区间价格预估。

**Architecture:** 能力层 = 新增 `services/generation/token_estimator.py`(纯函数:参数→预计 token→区间金额,复用 `cost.py` 价格档位公开入口,不重复定义价格);消费点 = `POST /tasks/estimate` handler(薄封装,复用 `validate_config` 校验,纯计算无副作用)。估算常量挂观测校准闭环(离线校准自 R1 live 实测,单点校准消费方零改动)。

**Tech Stack:** FastAPI / pydantic v2 / SQLAlchemy 2 / pytest / ruff / mypy(strict)。

**Spec:** `docs/superpowers/specs/2026-08-12-cost-observability-estimate-design.md`

## Global Constraints

- 工作目录:所有命令在 `main/` 下执行(`cd /home/kbzz1/shanka_backend/main`);conda 环境统一 `conda run -n shanka-backend ...`。
- 四工具全绿才允许 commit:`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`(ruff line-length 100)。
- 红线 1:app/schemas ↔ openapi.yaml ↔ structure-contract.md 三处一致;契约变更从 structure-contract 发起。
- 预估接口豁免幂等键(与 `/samples` 先例一致,契约 1.3);不需要 API Key;不落库、不出 Prometheus 指标(纯计算,spec 3.3 边界)。
- 请求体字段名为 `chapter_ids`(与 TaskCreateRequest 一致,非 spec 草稿中的 `selected_chapters`)。
- 知识点口径与 V4 规划完全一致:每章 3 × 密度系数(COMPACT=1/BALANCED=2/EXTENSIVE=3),每知识点一卡(契约 3.5、planning.py 同口径)。
- 完成后更新 `docs/Progress.md`(Task 5)与 `docs/frontend/handoff/handoff-2026-08-12.md`(Task 6 交接文档收尾)。
- **live 冒烟(Task 4)不进 pytest 套件**:自动化测试保持确定性与零网络(LOCAL-DONE 红线);真实调用只在验收时显式执行,固定 3 次、预算守卫 ¥0.5 封顶,从仓库根 `.env` 加载真实 Key。

---

### Task 1: token 用量估算模型(token_estimator.py)

**Files:**
- Create: `main/services/generation/token_estimator.py`
- Test: `main/tests/unit/test_token_estimator.py`

**Interfaces:**
- Consumes: `services/generation/cost.py::estimate_cost_by_kind(cache_hit_tokens, cache_miss_tokens, output_tokens, *, effective_date) -> dict[str, float]`(键 `cache_hit/cache_miss/output/total`,已有,不动)
- Produces:
  - `estimate_tokens(chapter_count: int, quantity_tendency: str, custom_requirements: str | None) -> dict[str, int]` — 键 `knowledge_point_count / estimated_card_count / prompt_tokens / output_tokens`
  - `estimate_price_range(chapter_count: int, quantity_tendency: str, custom_requirements: str | None, *, effective_date: str) -> dict[str, float | int | str]` — 键 `knowledge_point_count / estimated_card_count / price_low / price_high / currency`

- [ ] **Step 1: 写失败测试**

创建 `main/tests/unit/test_token_estimator.py`:

```python
"""token 估算模型单元测试(Agent 成本观测能力层,spec 3.1/3.2)。"""

from services.generation.token_estimator import (
    OUTPUT_TOKENS_PER_KP,
    PROMPT_TOKENS_PER_KP,
    estimate_price_range,
    estimate_tokens,
)


def test_density_matches_v4_planning() -> None:
    # 每章 3×密度系数(COMPACT=1/BALANCED=2/EXTENSIVE=3),2 章 = 6/12/18(V4 实测口径)
    assert estimate_tokens(2, "COMPACT", None)["knowledge_point_count"] == 6
    assert estimate_tokens(2, "BALANCED", None)["knowledge_point_count"] == 12
    assert estimate_tokens(2, "EXTENSIVE", None)["knowledge_point_count"] == 18


def test_unknown_tendency_falls_back_balanced() -> None:
    # 与 planning._DENSITY.get(tendency, 2) 同口径:未知值防御性回落 BALANCED
    assert estimate_tokens(1, "BALANCED", None)["knowledge_point_count"] == estimate_tokens(
        1, "UNKNOWN", None
    )["knowledge_point_count"]


def test_token_constants_and_card_count() -> None:
    t = estimate_tokens(1, "COMPACT", None)  # 1 章 COMPACT = 3 知识点
    assert t["estimated_card_count"] == 3
    assert t["prompt_tokens"] == 3 * PROMPT_TOKENS_PER_KP
    assert t["output_tokens"] == 3 * OUTPUT_TOKENS_PER_KP


def test_custom_requirements_add_prompt_tokens() -> None:
    base = estimate_tokens(1, "COMPACT", None)["prompt_tokens"]
    with_custom = estimate_tokens(1, "COMPACT", "a" * 10)["prompt_tokens"]  # 10 字符 × 0.5 = 5 token
    assert with_custom == base + 5


def test_price_range_exact_values() -> None:
    # 1 章 COMPACT = 3 KP:prompt 4500 / output 9900(deepseek-v4-flash,2026-08-12 价格档)
    # low = 4500×0.5/M + 9900×8/M = 0.00225 + 0.0792 = 0.08145
    # high = 4500×2/M + 9900×8/M = 0.009 + 0.0792 = 0.0882
    r = estimate_price_range(1, "COMPACT", None, effective_date="2026-08-12")
    assert r["knowledge_point_count"] == 3
    assert r["estimated_card_count"] == 3
    assert r["price_low"] == 0.08145
    assert r["price_high"] == 0.0882
    assert r["currency"] == "CNY"
    assert 0 < r["price_low"] <= r["price_high"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_token_estimator.py -v`
Expected: FAIL(ModuleNotFoundError / import 错误)

- [ ] **Step 3: 实现 token_estimator.py**

创建 `main/services/generation/token_estimator.py`:

```python
"""token 估算模型:Agent 成本/用量观测能力层(事前预估,spec 3.1/3.2)。

估算常量 = 对既有观测数据(Batch 表 cache_hit/miss/output 实际 token,8.3 Cache 指标)
的离线校准值——校准日期与依据见常量注释;换模型/换书籍/实际用量漂移时单点重新校准,
消费方零改动(校准闭环)。价格不在此定义:复用 cost.py 价格档位公开入口(8.4)。
"""

from typing import Any

from services.generation.cost import estimate_cost_by_kind

# 校准常量(2026-08-12,R1 live 实测 deepseek-v4-flash;向上取整偏保守)
PROMPT_TOKENS_PER_KP = 1500  # 实测 1,427/单元(85,599/60)
OUTPUT_TOKENS_PER_KP = 3300  # 实测 3,263/单元(195,774/60)
CUSTOM_REQ_TOKENS_PER_CHAR = 0.5  # 约定:custom_requirements 每字符约 0.5 token

_DENSITY_MULTIPLIER = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}
_BASE_CHUNKS_PER_CHAPTER = 3  # 与 planning._BASE_CHUNKS 同口径(每章基础分块数)


def estimate_tokens(
    chapter_count: int,
    quantity_tendency: str,
    custom_requirements: str | None,
) -> dict[str, int]:
    """输入参数 → 预计 token(与 V4 规划同口径:每章 3×密度系数知识点,每知识点一卡)。"""
    multiplier = _DENSITY_MULTIPLIER.get(quantity_tendency, _DENSITY_MULTIPLIER["BALANCED"])
    kp_count = chapter_count * _BASE_CHUNKS_PER_CHAPTER * multiplier
    prompt_tokens = kp_count * PROMPT_TOKENS_PER_KP
    if custom_requirements:
        prompt_tokens += int(len(custom_requirements) * CUSTOM_REQ_TOKENS_PER_CHAR)
    return {
        "knowledge_point_count": kp_count,
        "estimated_card_count": kp_count,
        "prompt_tokens": prompt_tokens,
        "output_tokens": kp_count * OUTPUT_TOKENS_PER_KP,
    }


def estimate_price_range(
    chapter_count: int,
    quantity_tendency: str,
    custom_requirements: str | None,
    *,
    effective_date: str,
) -> dict[str, Any]:
    """区间估值(8.3 hit/miss 边界):price_low=全命中缓存,price_high=全未命中;output 固定价。

    复用 cost.py 公开入口(生效日期取档),不触碰私有 _price_for、不重复定义价格。
    """
    tokens = estimate_tokens(chapter_count, quantity_tendency, custom_requirements)
    low = estimate_cost_by_kind(
        tokens["prompt_tokens"], 0, tokens["output_tokens"], effective_date=effective_date
    )
    high = estimate_cost_by_kind(
        0, tokens["prompt_tokens"], tokens["output_tokens"], effective_date=effective_date
    )
    return {
        "knowledge_point_count": tokens["knowledge_point_count"],
        "estimated_card_count": tokens["estimated_card_count"],
        "price_low": low["total"],
        "price_high": high["total"],
        "currency": "CNY",
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/unit/test_token_estimator.py -v`
Expected: PASS(5 passed)。再跑 `conda run -n shanka-backend python -m mypy services/generation/token_estimator.py` Expected: Success。

- [ ] **Step 5: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add main/services/generation/token_estimator.py main/tests/unit/test_token_estimator.py && git commit -m "feat: token 用量估算模型(Agent 成本观测能力层,离线校准常量 + 区间估值)"
```

---

### Task 2: 契约三处一致(CostEstimateRequest/Response + openapi + structure-contract)

**Files:**
- Modify: `main/app/schemas/tasks.py`(追加 2 个模型)
- Modify: `docs/Architecture/openapi.yaml`(/tasks/estimate 路径 + 2 个 components + responses 引用)
- Modify: `docs/Architecture/structure-contract.md`(8.4 能力口径 + 6.4 接口行)
- Test: `main/tests/contract/test_generation_schemas_guard.py`(追加 2 个守卫)

**Interfaces:**
- Consumes: `app/schemas/samples.py::GenerationConfig`(已有), `tests/contract/support.py::check_schema_consistency / load_openapi / openapi_schema`(已有)
- Produces:
  - `app.schemas.tasks.CostEstimateRequest` — pydantic 模型:`chapter_ids: list[str] = Field(min_length=1)`、`generation_config: GenerationConfig`
  - `app.schemas.tasks.CostEstimateResponse` — `knowledge_point_count: int`、`estimated_card_count: int`、`price_low: float`、`price_high: float`、`currency: str`

- [ ] **Step 1: 写失败守卫测试**

在 `main/tests/contract/test_generation_schemas_guard.py` 追加(文件顶部 import 处追加):

```python
from app.schemas.tasks import CostEstimateRequest, CostEstimateResponse
```

文件末尾追加:

```python
def test_cost_estimate_request_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        CostEstimateRequest, openapi_schema("CostEstimateRequest"), load_openapi()
    )
    assert violations == []


def test_cost_estimate_response_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        CostEstimateResponse, openapi_schema("CostEstimateResponse"), load_openapi()
    )
    assert violations == []
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/test_generation_schemas_guard.py -v`
Expected: FAIL(openapi 无 CostEstimateRequest 组件,check_schema_consistency 报缺失)

- [ ] **Step 3: 在 app/schemas/tasks.py 追加模型**

文件末尾(import 已有 `from app.schemas.samples import GenerationConfig`)追加:

```python
class CostEstimateRequest(BaseModel):
    """价格预估请求(spec 4:与 TaskCreateRequest 同构子集——仅章节+配置;纯计算,豁免幂等键)。"""

    chapter_ids: list[str] = Field(min_length=1)
    generation_config: GenerationConfig


class CostEstimateResponse(BaseModel):
    """价格预估响应(区间估值,单位元 CNY;spec 4/6.x)。"""

    knowledge_point_count: int
    estimated_card_count: int
    price_low: float
    price_high: float
    currency: str
```

- [ ] **Step 4: openapi.yaml 新增路径与组件**

`docs/Architecture/openapi.yaml` 的 `/tasks` 路径之后、`/tasks/{task_id}` 之前插入(注意:该文件 paths 用 2 空格缩进,先 Read 附近段落确认格式再插):

```yaml
  /tasks/estimate:
    post:
      tags: [tasks]
      summary: 创建任务前价格预估(区间估值:全命中~全未命中,单位元;纯计算,豁免幂等键)
      operationId: estimateTaskCost
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CostEstimateRequest'
      responses:
        '200':
          description: 预估成功(price_low=全部 prompt 命中缓存,price_high=全部未命中)
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CostEstimateResponse'
        '422':
          $ref: '#/components/responses/Unprocessable'
```

`components/schemas` 中 `TaskCreateRequest` 附近追加(先 Read 该组件定义,字段格式与其一致):

```yaml
    CostEstimateRequest:
      type: object
      required: [chapter_ids, generation_config]
      properties:
        chapter_ids:
          type: array
          minItems: 1
          items:
            type: string
            format: uuid
        generation_config:
          $ref: '#/components/schemas/GenerationConfig'
    CostEstimateResponse:
      type: object
      required: [knowledge_point_count, estimated_card_count, price_low, price_high, currency]
      properties:
        knowledge_point_count:
          type: integer
        estimated_card_count:
          type: integer
        price_low:
          type: number
        price_high:
          type: number
        currency:
          type: string
```

注意:先确认该文件 422 响应的既有引用写法(可能不是 `#/components/responses/ValidationError`),与 `/tasks` post 的 responses 块保持一致;若 `ValidationError` 组件不存在,照抄 `/tasks` 的 422 写法。

- [ ] **Step 5: 运行守卫确认通过**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/contract/test_generation_schemas_guard.py -v`
Expected: PASS(原 5 个 + 新 2 个)

- [ ] **Step 6: structure-contract.md 同步(8.4 能力口径 + 6.4 接口)**

`docs/Architecture/structure-contract.md`:

(a) 8.4 成本观测(O-6)小节末尾追加能力口径:

```markdown
- **token 用量估算模型(事前预估)**:估算常量 = 对 8.3/6.2 观测数据(Batch 实际 token)的离线校准值——`PROMPT_TOKENS_PER_KP=1500` / `OUTPUT_TOKENS_PER_KP=3300`(2026-08-12 校准自 R1 live 实测,向上取整偏保守)、`custom_requirements` 每字符 ≈0.5 token;换模型/换书籍时单点重新校准,消费方零改动。
- 预估输入映射与 V4 规划同口径:知识点数 = 章节数 × 3 × 密度系数(`COMPACT=1/BALANCED=2/EXTENSIVE=3`),每知识点一卡。
- 区间口径(8.3 hit/miss 边界):`price_low` = 全部 prompt 命中缓存(hit ratio 100%),`price_high` = 全部未命中(0%);output 固定价;复用本节约价档位。
```

(b) 6.4 任务接口表(或该小节内接口清单)追加一行:

```markdown
| POST | `/v1/tasks/estimate` | 创建任务前价格预估(区间估值,单位元):请求 `{chapter_ids, generation_config}`(空章节/非法配置 → 422,校验与 POST /tasks 同);响应 `{knowledge_point_count, estimated_card_count, price_low, price_high, currency}`;纯计算、不落库、**豁免幂等键**(与 6.3 /samples 同)、不需要 API Key | 豁免 |
```

- [ ] **Step 7: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add main/app/schemas/tasks.py main/tests/contract/test_generation_schemas_guard.py docs/Architecture/openapi.yaml docs/Architecture/structure-contract.md && git commit -m "contract: 任务价格预估(CostEstimate 组件三处一致 + 8.4 估算口径 + 6.4 接口)"
```

---

### Task 3: POST /tasks/estimate handler + 集成测试

**Files:**
- Modify: `main/app/api/tasks.py`(追加端点;import 追加)
- Test: `main/tests/integration/test_tasks_api.py`(追加测试;先 Read 该文件确认 client/device fixture 模式,复用同一模式)

**Interfaces:**
- Consumes: `app.schemas.tasks.CostEstimateRequest/CostEstimateResponse`(Task 2)、`services.generation.validate::validate_config(config: dict) -> None`(抛 AppError VALIDATION_ERROR)、`services.generation.token_estimator::estimate_price_range`(Task 1)、`infra.clock.SystemClock`、`infra.db.session.format_utc`
- Produces: `POST /v1/tasks/estimate` 端点(200;422;无副作用)

- [ ] **Step 1: 写失败集成测试**

在 `main/tests/integration/test_tasks_api.py` 追加(复用文件既有 fixture 与 helper:`ctx: tuple[TestClient, Path]`、`_device()`、`_uuid()`;文件顶部 `from infra.db.models import ...` 追加 `KnowledgePoint`):

```python
def test_tasks_estimate_returns_price_range(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    device = _device()
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [_uuid() for _ in range(2)],
            "generation_config": {
                "quantity_tendency": "EXTENSIVE",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers=device,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 2 章 EXTENSIVE = 18 知识点(V4 口径),每知识点一卡
    assert body["knowledge_point_count"] == 18
    assert body["estimated_card_count"] == 18
    assert body["currency"] == "CNY"
    assert 0 < body["price_low"] <= body["price_high"]


def test_tasks_estimate_empty_chapters_422(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [],
            "generation_config": {
                "quantity_tendency": "BALANCED",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers=_device(),
    )
    assert resp.status_code == 422


def test_tasks_estimate_invalid_config_422(ctx: tuple[TestClient, Path]) -> None:
    client, _ = ctx
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [_uuid()],
            "generation_config": {
                "quantity_tendency": "BOGUS",
                "difficulty_ratio": {"basic": 1.0, "understanding": 0.0, "application": 0.0},
            },
        },
        headers=_device(),
    )
    assert resp.status_code == 422


def test_tasks_estimate_no_side_effects(ctx: tuple[TestClient, Path]) -> None:
    # 无需 API Key(纯计算);预估不落库:任务/批次/知识点表零写入
    client, db_path = ctx
    device = _device()
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [_uuid()],
            "generation_config": {
                "quantity_tendency": "COMPACT",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers=device,
    )
    assert resp.status_code == 200, resp.text
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        assert session.scalar(select(Task).where(Task.device_id == device["X-Device-ID"])) is None
        assert session.scalar(select(KnowledgePoint)) is None


def test_tasks_estimate_without_idempotency_key(ctx: tuple[TestClient, Path]) -> None:
    # 豁免幂等键(spec 4:/samples 先例):不带 Idempotency-Key 正常 200
    client, _ = ctx
    resp = client.post(
        "/tasks/estimate",
        json={
            "chapter_ids": [_uuid()],
            "generation_config": {
                "quantity_tendency": "COMPACT",
                "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
            },
        },
        headers=_device(),
    )
    assert resp.status_code == 200, resp.text
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_tasks_api.py -v`
Expected: FAIL(404 或路由不存在)

- [ ] **Step 3: 实现端点**

`main/app/api/tasks.py`:

(a) import 追加(顶部现有 import 块中):

```python
from app.schemas.tasks import CostEstimateRequest, CostEstimateResponse
from services.generation.token_estimator import estimate_price_range
from services.generation.validate import validate_config
```

(b) 文件末尾(`list_task_batches_endpoint` 之后)追加:

```python
@router.post("/estimate", response_model=CostEstimateResponse)
def estimate_task_cost_endpoint(
    request: Request,
    payload: CostEstimateRequest,
) -> JSONResponse:
    """任务价格预估(6.x/spec 4):纯计算、无副作用、豁免幂等键、不需要 API Key。

    章节数 = len(chapter_ids) 纯计数(不做归属校验——创建任务时才校验);
    generation_config 复用 validate_config(422);金额按当天价格档位取档。
    """
    validate_config(payload.generation_config.model_dump())
    result = estimate_price_range(
        chapter_count=len(payload.chapter_ids),
        quantity_tendency=payload.generation_config.quantity_tendency,
        custom_requirements=payload.generation_config.custom_requirements,
        effective_date=format_utc(SystemClock().now_utc())[:10],
    )
    return JSONResponse(content=result)
```

- [ ] **Step 4: 运行确认通过 + 全量回归**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_tasks_api.py -v`
Expected: PASS。然后四工具全量:`python -m pytest -q`(366+ 全绿)、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`。

- [ ] **Step 5: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add main/app/api/tasks.py main/tests/integration/test_tasks_api.py && git commit -m "feat: POST /tasks/estimate 任务价格预估(区间估值,豁免幂等键,纯计算)"
```

---

### Task 4: 价格预估 live 冒烟(轻量真实调用)

> 目的:估算常量是离线校准值(1500/3300),必须用真实 DeepSeek 调用验证其贴近程度——每次验收执行一次,固定 **3 次**真实 chat(单知识点单元,不同难度),预算守卫 ¥0.5 封顶。**不进 pytest 套件**(确定性/零网络红线),本任务是显式验收步骤。

**Files:**
- Create: `main/scripts/live_estimate_smoke.py`

**Interfaces:**
- Consumes: `infra.llm.deepseek::DeepSeekClient(settings, api_key=...)`(构造注入,chat 空参)、`infra.llm.prompts::load_asset(section, name)` / `build_generation_prompt(prompt_asset, *, topic, chapter_name, difficulty, custom_requirements, card_schema)`、`services.generation.cost::estimate_cost_by_kind`、`services.generation.token_estimator` 常量(Task 1)
- Produces: 退出码 0(完成)/2(缺 Key 或预算超限);打印实际 token 均值/金额/偏差对照

- [ ] **Step 1: 写冒烟脚本**

创建 `main/scripts/live_estimate_smoke.py`(脚本位于 main/scripts/,运行时 CWD=main/;仓库根 .env 手动注入——与 scripts/run.sh 的 `source ../.env` 同源):

```python
"""live_estimate_smoke.py:价格预估轻量冒烟(真实 DeepSeek 调用,每次验收执行一次)。

对照:services/generation/token_estimator.py 估算常量(PROMPT_TOKENS_PER_KP=1500 /
OUTPUT_TOKENS_PER_KP=3300)。3 个单知识点单元(不同难度)真实 chat,记录 prompt/output
token,均值对照常量并报告偏差;实际金额对照预估区间(price_low/price_high 同口径)。

纪律:不进 pytest 套件(自动化测试确定性零网络,LOCAL-DONE 红线);固定 3 次调用,
预算守卫 ¥0.5 封顶(保险丝);从仓库根 .env 加载真实 Key。
用法:cd main && conda run -n shanka-backend python scripts/live_estimate_smoke.py
"""

import os
import sys
from pathlib import Path

MAIN_DIR = Path(__file__).resolve().parent  # main/
sys.path.insert(0, str(MAIN_DIR))
ROOT_ENV = MAIN_DIR.parent / ".env"
MAX_COST_YUAN = 0.5
SAMPLES: list[tuple[str, str, str]] = [
    ("AI Agent 定义与核心能力", "第一章 引言", "BASIC"),
    ("记忆与反思机制", "第二章 记忆", "UNDERSTANDING"),
    ("多 Agent 协作与工具调用场景", "第三章 协作", "APPLICATION"),
]


def _load_root_env() -> None:
    """仓库根 .env(DEEPSEEK_API_KEY)注入环境变量(与 scripts/run.sh 同源)。"""
    if ROOT_ENV.exists():
        for line in ROOT_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    _load_root_env()
    from app.config import Settings
    from infra.llm.deepseek import DeepSeekClient
    from infra.llm.prompts import build_generation_prompt, load_asset
    from services.generation.cost import estimate_cost_by_kind
    from services.generation.token_estimator import (
        OUTPUT_TOKENS_PER_KP,
        PROMPT_TOKENS_PER_KP,
    )

    settings = Settings()
    if not settings.deepseek_api_key:
        print("缺少 DEEPSEEK_API_KEY(仓库根 .env);中止", file=sys.stderr)
        return 2
    client = DeepSeekClient(settings, api_key=settings.deepseek_api_key)
    prompt_asset = load_asset("prompts", "generator")
    card_schema = load_asset("schemas", "card")

    print("=== 价格预估 live 冒烟(单知识点单元 x3,真实调用)===")
    total_prompt = 0
    total_output = 0
    for topic, chapter, difficulty in SAMPLES:
        prompt = build_generation_prompt(
            prompt_asset,
            topic=topic,
            chapter_name=chapter,
            difficulty=difficulty,
            custom_requirements=None,
            card_schema=card_schema,
        )
        result = client.chat(prompt)  # 明文 Key 构造时注入(executor 同款)
        usage = result["usage"]
        hit: int = int(usage.get("prompt_cache_hit_tokens") or 0)
        miss: int = int(usage.get("prompt_cache_miss_tokens") or 0)
        out: int = int(usage.get("completion_tokens") or 0)
        total_prompt += hit + miss
        total_output += out
        print(f"{difficulty:<13} prompt={hit + miss:>5}(hit {hit}/miss {miss}) output={out:>5}")

    avg_prompt = total_prompt / len(SAMPLES)
    avg_output = total_output / len(SAMPLES)
    # 实际金额:3 次均为新样本首调,保守按全 miss 口径
    actual = estimate_cost_by_kind(0, total_prompt, total_output, effective_date="2026-08-12")
    low = estimate_cost_by_kind(total_prompt, 0, total_output, effective_date="2026-08-12")
    high = estimate_cost_by_kind(0, total_prompt, total_output, effective_date="2026-08-12")
    print(f"均值 prompt={avg_prompt:.0f}(常量 {PROMPT_TOKENS_PER_KP}) "
          f"output={avg_output:.0f}(常量 {OUTPUT_TOKENS_PER_KP})")
    print(f"金额:实际(全 miss)¥{actual['total']:.4f} "
          f"区间 ¥{low['total']:.4f}~¥{high['total']:.4f}")
    if actual["total"] > MAX_COST_YUAN:
        print(f"预算超限 ¥{actual['total']:.4f} > ¥{MAX_COST_YUAN};中止", file=sys.stderr)
        return 2
    prompt_drift = (avg_prompt - PROMPT_TOKENS_PER_KP) / PROMPT_TOKENS_PER_KP
    output_drift = (avg_output - OUTPUT_TOKENS_PER_KP) / OUTPUT_TOKENS_PER_KP
    print(f"偏差:prompt {prompt_drift:+.1%} output {output_drift:+.1%}")
    if abs(prompt_drift) > 0.2 or abs(output_drift) > 0.2:
        print("提示:偏差 >20%,评估是否校准 token_estimator 常量(离线校准,登记后修改)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 静态检查(不执行)**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m ruff check scripts/live_estimate_smoke.py && conda run -n shanka-backend python -m mypy scripts/live_estimate_smoke.py`
Expected: ruff 通过;mypy Success(若 mypy 报类型问题按提示修正——脚本在 main/ 下,mypy . 覆盖)

- [ ] **Step 3: 执行冒烟(真实调用,3 次,预算 <¥0.5)**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python scripts/live_estimate_smoke.py`
Expected: 打印 3 行样本 usage + 均值/金额/偏差;退出码 0。**把输出原文记录到 Task 5 的 Progress R22 小节**(实际均值、偏差 %、金额)。若偏差 >20%:登记观察,不自动改常量(校准闭环:记录后人工决策)。

- [ ] **Step 4: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add main/scripts/live_estimate_smoke.py && git commit -m "feat: 价格预估 live 冒烟脚本(3 次真实调用,预算封顶,对照估算常量)"
```

---

### Task 5: Progress.md 更新 + 全量回归

**Files:**
- Modify: `docs/Progress.md`

- [ ] **Step 1: Progress.md 更新**

在 `## 4. 依赖驱动工作包` 末尾新增工作包小节(格式参照既有包,含验收实测记录——以 Task 1~3 实际测试输出为准):

```markdown
### R22 — Agent 成本观测能力层 + 任务价格预估接口

**`DONE`｜依赖：V5A｜覆盖：契约 8.4 扩展、6.4、红线 1｜2026-08-12**

- **能力层**（services/generation/token_estimator.py）：token 用量估算模型——常量挂观测校准闭环（PROMPT_TOKENS_PER_KP=1500 / OUTPUT_TOKENS_PER_KP=3300，2026-08-12 校准自 R1 live 实测 1,427/3,263，向上取整偏保守；custom_requirements 每字符 ≈0.5）；输入映射与 V4 规划同口径（每章 3×密度系数，每知识点一卡）；区间估值复用 cost.py 价格档位公开入口（low=全命中 / high=全未命中，output 固定价），不重复定义价格。
- **消费点**（POST /v1/tasks/estimate）：`{chapter_ids, generation_config}` → `{knowledge_point_count, estimated_card_count, price_low, price_high, currency}`；复用 validate_config（422）；纯计算、不落库、豁免幂等键、不需要 API Key。
- **契约**：structure-contract 8.4 能力口径 + 6.4 接口行；openapi /tasks/estimate + CostEstimateRequest/Response 组件；schemas 守卫锚点，三处一致。
- **live 冒烟**（Task 4 输出原文贴此）：3 次真实调用 prompt 均值 <N> / output 均值 <N>（常量 1500/3300，偏差 ±X%）；实际金额 ¥<N>(全 miss 口径)落在/超出区间 ¥<N>~¥<N>。
- 验收实测：四工具全绿（<N> passed、mypy <N> files）；<边界用例数> 用例全绿；预估无副作用（任务表零写入）。
```

- [ ] **Step 3: 全量四工具回归**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .`
Expected: 全部通过。

- [ ] **Step 4: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add docs/Progress.md && git commit -m "docs: Progress 登记 R22(Agent 成本观测能力层 + 任务价格预估)"
```

---

### Task 6: 更新前端交接文档(handoff)

> 收尾义务:价格预估接口上线后,前端交接文档必须同步,否则前端按旧文档对接会漏接新接口。

**Files:**
- Modify: `docs/frontend/handoff/handoff-2026-08-12.md`

- [ ] **Step 1: §1 联调状态总览——Mock AI 行关闭**

`docs/frontend/handoff/handoff-2026-08-12.md` §1 表格 `Mock AI / staging 测试环境` 行改为:

```markdown
| Mock AI / staging 测试环境 | ✅ 已关闭 | 方向变更:不做 mock/fake,全量走真实 API Key 联调;前端待办 #5 相应改写(见 §6) |
```

- [ ] **Step 2: §4 新增接口表格——追加预估接口**

§4 表格(`PATCH /decks/{deck_id}` 等 4 行之后)追加一行:

```markdown
| `POST /tasks/estimate` | 创建任务前价格预估(区间估值,单位元):请求 `{chapter_ids, generation_config}`(与创建任务同构子集);响应 `{knowledge_point_count, estimated_card_count, price_low, price_high, currency}`;纯计算不落库、豁免幂等键、无需 API Key | 豁免 |
```

- [ ] **Step 3: §6 前端待办清单——#5 改写**

§6 前端待办 #5 改为:

```markdown
5. **创建任务前价格预估**：调 `POST /tasks/estimate`(传已选章节 + 生成配置)展示「预计 ¥X ~ ¥Y」(price_low=全部命中缓存的乐观下限,price_high=全部未命中的保守上限,单位元)；无需真实 Key 即可调用；创建任务前先给用户看到成本区间再确认。
```

- [ ] **Step 4: 新增 §7 价格预估对接说明**(文档末尾追加)

```markdown
## 7. 价格预估接口对接说明（2026-08-12 新增）

- **用途**：创建任务前向用户展示预计花费（用户自持 Key，成本敏感）。
- **语义**：纯计算、不落库、不消耗 Key/余额、豁免幂等键（`/samples` 先例）；`chapter_ids` 只做计数（归属校验在创建任务时）。
- **区间含义**：`price_low` = 全部 prompt 命中缓存（乐观下限），`price_high` = 全部未命中（保守上限），真实成本落在区间内；`output` 按固定价。
- **估算依据**：知识点数 = 章节数 × 3 × 密度系数（COMPACT=1/BALANCED=2/EXTENSIVE=3），每知识点一卡；单价常量在后端 `cost.py`（前端不硬编码价格）。
- **错误**：`chapter_ids` 为空或 `generation_config` 非法 → 422（与创建任务同口径）。
```

- [ ] **Step 5: Commit**

```bash
cd /home/kbzz1/shanka_backend && git add docs/frontend/handoff/handoff-2026-08-12.md && git commit -m "docs: 前端交接——价格预估接口对接说明 + Mock AI 方向关闭"
```
