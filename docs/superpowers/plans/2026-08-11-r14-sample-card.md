# R-14 SampleCard 轻量组件实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 SampleCard 轻量组件（R-14 清账）：structure-contract 新增资源模型 3.13、openapi 新增 SampleCard 组件并替换 /samples 响应 items 引用、schemas 新增守卫锚点模型、handler 移除合成占位字段，三处一致（红线 1），测试与守卫同步。

**Architecture:** /samples 响应从 Card 全量结构改为轻量 SampleCard——删除落库/归属/版本语义字段（deck_id、position、source、generation_item_id、knowledge_point_ids、rubric 四维+总分、version、created_at、updated_at），保留客户端预览所需字段。样卡不入库（database-design 不动）。fake 生成器不动（其 dict 是超集，handler 显式映射子集）。

**Tech Stack:** FastAPI + pydantic v2（现有）；openapi.yaml 手维护（无代码生成）；pytest 契约守卫（tests/contract/support.py 锚点模式）。

## Global Constraints

- 根 AGENTS.md 红线 1：`app/schemas/` ↔ `openapi.yaml` ↔ `structure-contract.md` 三处一致；变更从结构契约发起。
- 防漂移规则 5：资源模型变更 → openapi schema + 数据库表；样卡不入库，database-design.md 无改动。
- structure-contract 6.3：样卡不入库、不参与统计、豁免幂等键——行为不变，仅响应结构。
- openapi `/samples` 响应 `required: [sample_cards]`、`minItems/maxItems: 3` 保留。
- Card 组件其余 4 处引用（/decks 卡片相关）一律不动。
- 四工具全绿门槛（main/ 下）：pytest、ruff check、ruff format --check、mypy strict。
- fake.py 及其返回字段（generation_item_id 等超集字段）不改——V5A 后仍被 samples service 与测试使用。

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `docs/Architecture/structure-contract.md` | 修改 | 新增 3.13 SampleCard 资源模型；6.3 样卡"响应直接返回卡片结构"表述同步 |
| `docs/Architecture/openapi.yaml` | 修改 | components/schemas 新增 SampleCard；/samples 响应 items 改 $ref SampleCard |
| `main/app/schemas/samples.py` | 修改 | 新增 SampleCard pydantic 模型（守卫锚点 + handler 序列化） |
| `main/app/api/samples.py` | 修改 | 移除 handler 合成占位（deck_id/position/created_at/updated_at），显式映射 SampleCard |
| `main/tests/contract/test_generation_schemas_guard.py` | 修改 | 新增 SampleCard ↔ openapi 一致性守卫用例 |
| `main/tests/integration/test_samples_api.py` | 修改 | 占位字段断言（约 108-111 行）→ SampleCard 字段断言 |
| `docs/Progress.md` | 修改 | R-14 状态 → RESOLVED（由主 Agent 在合并前更新，实施者不碰） |

不动：`database-design.md`、`fake.py`、`services/generation/samples.py`、openapi Card 其余引用。

## 契约设计（3.13 SampleCard）

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `card_id` | uuid | ✓ | 样卡预览标识（不入库，不保证跨请求稳定） |
| `front` / `back` | string | ✓ | 通用渲染字段（所有卡片） |
| `code` | string | ✗ | 卡片编号 |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE` |
| `question` / `answer` | string | ✗ | 仅 `QUESTION` 卡 |
| `statement` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `answer_boolean` | bool | ✗ | 仅 `TRUE_FALSE` 卡 |
| `explanation` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `target_difficulty` | enum | ✗ | `BASIC` / `UNDERSTANDING` / `APPLICATION`（体现难度差异，PRD 5.5） |

删除理由：deck_id/position（落库归属）、source（样卡恒为生成形态，无分类语义）、generation_item_id（不入库无防重语义）、knowledge_point_ids + rubric 四维 + rubric_total_score（不参与 Rubric 评估，PRD 5.5 数据规则）、version/created_at/updated_at（无版本与持久化时间语义）。

---

### Task 1: SampleCard 契约、模型、实现与测试（单任务闭环）

**Files:**
- Modify: `docs/Architecture/structure-contract.md`（3.12 之后新增 3.13；6.3 段落表述同步）
- Modify: `docs/Architecture/openapi.yaml`（components/schemas SampleCard + /samples items 引用；237 行附近）
- Modify: `main/app/schemas/samples.py`（追加 SampleCard 模型）
- Modify: `main/tests/contract/test_generation_schemas_guard.py`（新增守卫用例）
- Modify: `main/tests/integration/test_samples_api.py`（更新响应断言）
- Modify: `main/app/api/samples.py`（handler 映射）

**Interfaces:**
- Consumes: `services/generation/samples.py::generate_samples` 返回 `list[dict[str, object]]`（fake 超集字段）；`tests/contract/support.py` 的 `check_schema_consistency` / `openapi_schema`（锚点模式，已有 Task/KnowledgePoint/SampleRequest 先例）。
- Produces: `app.schemas.samples.SampleCard`（pydantic BaseModel，守卫锚点 + handler 序列化）；openapi 组件 `SampleCard`；structure-contract 3.13。

- [ ] **Step 1: structure-contract.md 新增 3.13 SampleCard**

在 3.12 StatsDashboard 之后追加（编号 3.13）：

```markdown
### 3.13 SampleCard

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `card_id` | uuid | ✓ | 样卡预览标识（不入库,不保证跨请求稳定） |
| `front` / `back` | string | ✓ | 通用渲染字段(所有卡片) |
| `code` | string | ✗ | 卡片编号 |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE` |
| `question` / `answer` | string | ✗ | 仅 `QUESTION` 卡 |
| `statement` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `answer_boolean` | bool | ✗ | 仅 `TRUE_FALSE` 卡 |
| `explanation` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `target_difficulty` | enum | ✗ | `BASIC` / `UNDERSTANDING` / `APPLICATION` |

与 Card 的差异：删去落库/归属/版本语义字段（deck_id、position、source、
generation_item_id、knowledge_point_ids、Rubric 四维与总分、version、created_at、
updated_at）——样卡不入库、不参与统计与 Rubric（PRD 5.5 数据规则），仅承载
前端预览所需结构。
```

并将 6.3 段落（约 406 行）`样卡不入库、不参与统计(响应直接返回卡片结构)。` 改为：
`样卡不入库、不参与统计(响应返回 SampleCard 轻量组件,见 3.13)。`

- [ ] **Step 2: openapi.yaml 新增 SampleCard 组件并替换 /samples 引用**

components/schemas 区（SampleRequest 组件附近，保持字母序——SampleCard 在 SampleRequest 之前）新增：

```yaml
    SampleCard:
      type: object
      required: [card_id, front, back, card_type]
      properties:
        card_id:
          type: string
          format: uuid
        front:
          type: string
        back:
          type: string
        code:
          type: string
        card_type:
          type: string
          enum: [QUESTION, TRUE_FALSE]
        question:
          type: string
        answer:
          type: string
        statement:
          type: string
        answer_boolean:
          type: boolean
        explanation:
          type: string
        target_difficulty:
          type: string
          enum: [BASIC, UNDERSTANDING, APPLICATION]
```

/samples 响应（约 237 行）`items: $ref: '#/components/schemas/Card'` → `items: $ref: '#/components/schemas/SampleCard'`。其余 4 处 Card 引用不动。

- [ ] **Step 3: app/schemas/samples.py 新增 SampleCard 模型**

在 SampleRequest 之后追加（锚点模型：必填字段用非 Optional 注解，与 openapi required 对齐；card_type/target_difficulty 用 str 不校验 enum 值集——既有口径，与 Task 视图一致）：

```python
class SampleCard(BaseModel):
    """样卡轻量组件（structure-contract 3.13；openapi SampleCard）。

    与 Card 的差异：删去落库/归属/版本语义字段——样卡不入库、不参与统计与
    Rubric（PRD 5.5 数据规则），仅承载前端预览所需结构。
    """

    card_id: str
    front: str
    back: str
    code: str | None = None
    card_type: str
    question: str | None = None
    answer: str | None = None
    statement: str | None = None
    answer_boolean: bool | None = None
    explanation: str | None = None
    target_difficulty: str | None = None
```

- [ ] **Step 4: 守卫新增用例（此时应绿）**

`main/tests/contract/test_generation_schemas_guard.py` 追加（import 增加 SampleCard）：

```python
def test_sample_card_schema_openapi_consistent() -> None:
    violations = check_schema_consistency(
        SampleCard, openapi_schema("SampleCard"), load_openapi()
    )
    assert violations == []
```

运行 `conda run -n shanka-backend python -m pytest tests/contract/test_generation_schemas_guard.py -q`，预期该用例 PASS。

- [ ] **Step 5: test_samples_api.py 断言更新（此时应红——handler 仍合成占位）**

`main/tests/integration/test_samples_api.py` 约 108-111 行，将：

```python
    # F-2：openapi Card required 字段齐全（deck_id/position/created_at/updated_at 由 handler 合成）
    for c in cards:
        assert {"deck_id", "position", "created_at", "updated_at"} <= set(c)
        assert c["deck_id"] == "" and c["position"] == 0
        assert c["created_at"] and c["updated_at"]
```

替换为：

```python
    # R-14：SampleCard 轻量组件（structure-contract 3.13）——无落库/归属/版本占位字段
    for c in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(c)
        assert {"deck_id", "position", "created_at", "updated_at"} & set(c) == set()
```

运行该文件，预期此用例 FAIL（handler 仍返回 deck_id 等占位字段）。

- [ ] **Step 6: handler 移除占位并映射 SampleCard（此时应绿）**

`main/app/api/samples.py`：删除 `_now()` 与合成循环，改为显式字段映射（取 fake 超集子集，与 3.13 字段一一对应）：

```python
from app.schemas.samples import SampleCard, SampleRequest

def _to_sample_card(card: dict[str, object]) -> SampleCard:
    """fake/生成器返回字段 → SampleCard 轻量组件（显式映射，剔除落库/归属/版本字段）。"""
    return SampleCard(
        card_id=str(card["card_id"]),
        front=str(card["front"]),
        back=str(card["back"]),
        card_type=str(card["card_type"]),
        statement=card.get("statement"),  # type: ignore[arg-type]
        answer_boolean=card.get("answer_boolean"),  # type: ignore[arg-type]
        explanation=card.get("explanation"),  # type: ignore[arg-type]
        target_difficulty=card.get("target_difficulty"),  # type: ignore[arg-type]
    )


@router.post("")
def generate_samples_endpoint(
    request: Request,
    payload: SampleRequest,
    session: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    cards = generate_samples(
        session,
        device_id=request.state.device_id,
        file_id=payload.file_id,
        chapter_ids=payload.chapter_ids,
        config=payload.generation_config.model_dump(),
    )
    return JSONResponse(
        content={"sample_cards": [_to_sample_card(c).model_dump() for c in cards]}
    )
```

（`# type: ignore[arg-type]` 为 Optional 字段注入 dict[str, object] 值所需——若 mypy 不报可删。`question/answer/code` 可选字段 fake 不返回，保持 None 即可，不显式传。）

- [ ] **Step 7: 全量验证四工具**

`cd main && conda run -n shanka-backend python -m pytest`（353+ 全绿，含更新的 samples 用例与新增守卫用例）；`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`。

- [ ] **Step 8: 提交**

```bash
git add docs/Architecture/structure-contract.md docs/Architecture/openapi.yaml main/app/schemas/samples.py main/app/api/samples.py main/tests/contract/test_generation_schemas_guard.py main/tests/integration/test_samples_api.py
git commit -m "feat(samples): SampleCard 轻量组件落地（R-14 清账——契约 3.13 + openapi + schemas 三处一致，handler 去占位）"
```

---

## Self-Review 核对

- **PRD 覆盖**：PRD 5.5 行为约束（不入库/不计统计/不参与 Rubric/不进复习）不变，仅响应结构轻量化；字段权威在 structure-contract（3.13 新增）——符合防漂移规则 2。
- **占位符扫描**：本计划所有代码/字段表均为最终值，无 TBD。
- **类型一致**：SampleCard 字段名在契约表 / openapi properties / pydantic 模型 / handler 映射四处的拼写一致（card_id、front、back、code、card_type、question、answer、statement、answer_boolean、explanation、target_difficulty）。
- **守卫兼容**：required 四项（card_id/front/back/card_type）非 Optional 注解 ↔ openapi required 列表对齐；可选字段 None 默认 ↔ openapi 无 required。
