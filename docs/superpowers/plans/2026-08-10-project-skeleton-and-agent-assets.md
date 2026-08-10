# 项目骨架与 agent 资产版本化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地已批准的规格（`docs/superpowers/specs/2026-08-10-project-skeleton-design.md`）：建 `main/` 纯空文件骨架、`agent_evolution/` 版本化资产目录 v1 首版、更新全部契约文档、编写 deployment.md。

**Architecture:** 契约驱动仓库编排。main 为空文件骨架（结构可追溯），agent_evolution 为目录快照 + manifest 的资产版本化管理，契约文档与部署设计同步定稿。本期不写任何可运行代码。

**Tech Stack:** Python 3.12（pyproject 声明）、ruff/mypy/pre-commit（工具链）、JSON Schema（卡片 schema 资产）、Markdown（契约文档）。

## Global Constraints

- 本期不写可运行代码；模块文件仅 `__init__.py`（docstring 注明对应契约章节）。
- 依赖方向不变：`docs/PRD → docs/Architecture → main/`；`agent_evolution/` 是 main 的资产源，不反向依赖。
- 资产版本化：目录快照 + manifest，**本期所有资产仅 v1 首版**，manifest 全指向 v1。
- 资产 v1 为"首版草稿"：从 PRD/契约推导，供 P2-4/P2-5 实现时精修，本期不追求最终质量。
- 观测范围仅 DeepSeek；Tunnel 实操推迟到 P3-4，本期只写 deployment.md 设计。
- 不建 CI、不引任务队列、不引 OpenTelemetry、不建测试用例（tests/ 只建目录）。
- 新文件命名：小写 + 连字符（与 docs/ 惯例一致）。

---

### Task 1: main 空文件骨架（目录 + 模块空文件 + pyproject + pre-commit）

**Files:**
- Create: `main/pyproject.toml`
- Create: `main/.pre-commit-config.yaml`
- Create: `main/` 下全部模块空文件（见下方清单）
- Create: `main/tests/unit/README.md`、`main/tests/integration/README.md`、`main/tests/contract/README.md`、`main/tests/acceptance/README.md`

**Interfaces:**
- Produces: `main/` 完整目录树（P0-0 交付物）；后续任务 3~5 更新的契约文档引用此结构。

- [ ] **Step 1: 创建目录树与全部模块空文件**

```bash
cd /home/kbzz1/shanka_backend
mkdir -p main/app/api main/app/middleware main/app/schemas \
  main/domain main/services/pdf main/services/generation main/services/scheduling \
  main/services/decks main/services/stats main/infra/db main/infra/storage main/infra/llm \
  main/tests/unit main/tests/integration main/tests/contract main/tests/acceptance
```

空文件清单（`__init__.py` 第一行为 docstring，注释对应契约章节；其余文件仅 docstring）：

| 文件 | docstring 内容（注明契约来源） |
| --- | --- |
| `main/app/__init__.py` | `"""app 应用层：HTTP 出入口（project-structure 3）"""` |
| `main/app/main.py` | `"""应用装配（空壳）"""` |
| `main/app/api/__init__.py` | `"""路由与 handler，按契约接口分组（structure-contract 6）"""` |
| `main/app/api/pdfs.py` | `"""PDF 接口（6.1）"""` |
| `main/app/api/api_key.py` | `"""API Key 接口（6.2）"""` |
| `main/app/api/samples.py` | `"""样卡接口（6.3）"""` |
| `main/app/api/tasks.py` | `"""任务接口（6.4）"""` |
| `main/app/api/decks.py` | `"""牌组接口（6.5）"""` |
| `main/app/api/cards.py` | `"""卡片与单卡重写接口（6.5/6.7）"""` |
| `main/app/api/review.py` | `"""复习接口（6.6）"""` |
| `main/app/api/stats.py` | `"""数据看板接口（6.8）"""` |
| `main/app/middleware/__init__.py` | `"""中间件（结构契约 1.1/1.3/1.4/1.6）"""` |
| `main/app/middleware/device_id.py` | `"""X-Device-ID 鉴权（1.1）"""` |
| `main/app/middleware/idempotency.py` | `"""Idempotency-Key 幂等（1.3）"""` |
| `main/app/middleware/error_handler.py` | `"""统一错误响应（1.4）"""` |
| `main/app/middleware/rate_limit.py` | `"""全局限流（1.6）"""` |
| `main/app/schemas/__init__.py` | `"""请求/响应模型，与 openapi.yaml 一致（红线 1）"""` |
| `main/app/schemas/common.py` | `"""通用 schema：错误响应、幂等头（1.3/1.4）"""` |
| `main/app/schemas/pdfs.py` / `api_key.py` / `samples.py` / `tasks.py` / `decks.py` / `cards.py` / `review.py` / `stats.py` | 与对应 api 文件同名引用契约接口章节 |
| `main/domain/__init__.py` | `"""领域模型：结构契约第 3 章资源模型（纯数据结构，无依赖）"""` |
| `main/domain/enums.py` | `"""全部枚举：状态、评级、类型、来源"""` |
| `main/domain/pdf_file.py` | `"""PdfFile/Chapter（契约 3.2/3.3）"""` |
| `main/domain/task.py` | `"""Task/GenerationConfig（3.4/3.5）"""` |
| `main/domain/knowledge_point.py` | `"""KnowledgePoint（3.6）"""` |
| `main/domain/batch.py` | `"""Batch（3.7）"""` |
| `main/domain/deck.py` | `"""Deck（3.8）"""` |
| `main/domain/card.py` | `"""Card（3.9）"""` |
| `main/domain/review_state.py` | `"""ReviewState（3.10）"""` |
| `main/domain/review_event.py` | `"""ReviewEvent（3.11）"""` |
| `main/domain/api_key.py` | `"""ApiKey（3.1）"""` |
| `main/services/__init__.py` + `pdf/__init__.py` + `generation/__init__.py` + `scheduling/__init__.py` + `decks/__init__.py` + `stats/__init__.py` | `"""用例层（project-structure 3）"""`（各子包注明职责：pdf=上传解析、generation=规划/生成/校验/Rubric/重写、scheduling=FSRS-6、decks=牌组/导入、stats=看板聚合） |
| `main/infra/__init__.py` + `db/__init__.py` + `storage/__init__.py` + `llm/__init__.py` | `"""基础设施（project-structure 3）"""`（db=ORM 与迁移、storage=PDF 存储、llm=DeepSeek 调用与 Prompt 资产加载，不落 Key） |

tests 四层 README 内容（每层一段，注明职责，与 project-structure 测试策略一致——该策略在 Task 3 写入契约）：
- `unit/README.md`：domain、schemas 纯逻辑单元测试。
- `integration/README.md`：services 编排、DB 事务边界（幂等同事务、级联删除、resume 并发）；易碎行为必须出现在此层。
- `contract/README.md`：守卫四项（见规格 9.4）：schemas↔openapi、ORM↔database-design、错误码↔契约第 7 章、localization_key↔文案。
- `acceptance/README.md`：AC-01~AC-11 验收用例映射（P3-2 回归）。

- [ ] **Step 2: 创建 `main/pyproject.toml`**

```toml
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
]

[project.optional-dependencies]
dev = [
  "pytest>=8.0",
  "ruff>=0.6",
  "mypy>=1.11",
  "pre-commit>=3.7",
]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = true
```

> 依赖锁定文件推迟到 P0-1 首次安装依赖时生成（uv lock 或等价机制）；pyproject 为唯一依赖事实源。P0-2 起按需追加 SQLAlchemy 等依赖。

- [ ] **Step 3: 创建 `main/.pre-commit-config.yaml`**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff-format
      - id: ruff
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.11.2
    hooks:
      - id: mypy
```

- [ ] **Step 4: 验证结构完整性**

```bash
cd /home/kbzz1/shanka_backend/main
# 断言关键文件存在
test -f app/main.py && test -f app/api/pdfs.py && test -f app/middleware/idempotency.py \
  && test -f domain/batch.py && test -f services/generation/__init__.py \
  && test -f infra/llm/__init__.py && test -f tests/contract/README.md && echo "结构 OK"
# 校验 pyproject 可解析
python3 -c "import tomllib; tomllib.load(open('pyproject.toml','rb')); print('pyproject OK')"
# 校验 pre-commit 配置为合法 YAML
python3 -c "import yaml; yaml.safe_load(open('.pre-commit-config.yaml')); print('pre-commit OK')"
```

Expected: 三行 OK 输出。

- [ ] **Step 5: 提交**

```bash
git add main/
git commit -m "feat(skeleton): main 空文件骨架 + pyproject/pre-commit 工具链（P0-0）"
```

---

### Task 2: agent_evolution 资产目录 v1 首版

**Files:**
- Create: `agent_evolution/manifest.json`
- Create: `agent_evolution/CHANGELOG.md`
- Create: `agent_evolution/prompts/v1/planner.md`
- Create: `agent_evolution/prompts/v1/generator.md`
- Create: `agent_evolution/schemas/v1/card.schema.json`
- Create: `agent_evolution/rubrics/v1/rubric.md`
- Create: `agent_evolution/rubrics/v1/scoring-prompt.md`

**Interfaces:**
- Produces: `manifest.json` 为运行时加载唯一入口（main/infra/llm 实现时按 path 加载）；`Batch.prompt_version/schema_version/rubric_version` 字段值 = manifest 中 version。

- [ ] **Step 1: 创建 `agent_evolution/manifest.json`**（本期全 v1）

```json
{
  "prompts": {
    "planner": { "version": "v1", "path": "prompts/v1/planner.md" },
    "generator": { "version": "v1", "path": "prompts/v1/generator.md" }
  },
  "schemas": {
    "card": { "version": "v1", "path": "schemas/v1/card.schema.json" }
  },
  "rubrics": {
    "main": { "version": "v1", "path": "rubrics/v1/rubric.md" }
  }
}
```

- [ ] **Step 2: 创建 `agent_evolution/CHANGELOG.md`**

```markdown
# agent_evolution 演进日志

## v1（2026-08-10）

- 初始资产：prompts（planner/generator）、schemas（card）、rubrics（main + scoring-prompt）。
- 来源：PRD v2.1（5.6/5.7/5.8/5.9）与结构契约（3.5/3.9）推导的首版草稿，P2-4/P2-5 实现时精修。
```

- [ ] **Step 3: 创建 `agent_evolution/prompts/v1/planner.md`**

内容要素（依据 PRD 5.6 / 契约 3.5、3.6）：
- 角色：教材知识点规划助手；任务：从选定章节原文分片中提取知识点清单。
- 输入：章节名称与页码范围、原文分片（source_chunk_id）、生成配置（quantity_tendency、difficulty_ratio、custom_requirements）。
- 输出：JSON 数组，每项 `{source_chunk_id, topic, priority}`；priority 为整数（1 最高），按知识重要性排序。
- 规则：
  - 粒度随 `quantity_tendency`：`COMPACT` 规划知识点数 ≤ `BALANCED` ≤ `EXTENSIVE`（同章节可测口径）；
  - 综合应用难度要求可合并相关原子知识点为主题（不强制一对一）；
  - 只输出 JSON，不输出解释；topic 用原文术语。

首版草稿全文（写入文件）：

```markdown
# 知识点规划 Prompt（v1）

你是教材知识点规划助手。给定章节原文分片与生成配置，产出知识点清单。

## 输入

- 章节：{name}（{start_page}–{end_page} 页）
- 分片：`{source_chunk_id}` 对应原文内容
- 配置：数量倾向 `{quantity_tendency}`、难度比例 `{difficulty_ratio}`、自定义要求 `{custom_requirements}`

## 任务

从分片中识别可作为闪卡生成单元的原子知识点，输出 JSON 数组，不输出其他内容。

## 输出格式

```json
[
  {"source_chunk_id": "<分片标识>", "topic": "<知识点主题>", "priority": 1}
]
```

## 规则

1. `priority` 为整数，1 为最高优先级，按知识重要性排序。
2. 知识点粒度：同章节下 `COMPACT` 数量 ≤ `BALANCED` 数量 ≤ `EXTENSIVE` 数量。
3. 综合应用难度（APPLICATION 比例 > 0）时，允许将相关原子知识点合并为主题；其余情况保持原子性。
4. `topic` 使用原文术语，不改写。
5. 分片无有效学习内容时，返回空数组。
```

- [ ] **Step 4: 创建 `agent_evolution/prompts/v1/generator.md`**

内容要素（依据 PRD 5.4.2 / 5.7 / 5.8 / 契约 3.9）：
- 角色：闪卡制作专家；任务：按知识点 + 配置生成单张卡片 JSON。
- 输入：知识点（topic/章节原文分片）、目标难度、卡类型选择规则。
- 输出：符合 `schemas/v1/card.schema.json` 的卡片 JSON。
- 规则：类型二选一（question / true_false，自动选择）；难度对应 BASIC/UNDERSTANDING/APPLICATION；判断题必须给 explanation；所有卡必填 front/back；内容严格基于原文；不输出多余内容。

首版草稿全文（写入文件）：

```markdown
# 分批生成 Prompt（v1）

你是闪卡制作专家。根据知识点与生成配置，制作一张符合 Schema 的闪卡。

## 输入

- 知识点：{topic}
- 原文分片：{source_chunk_id} 对应内容
- 目标难度：{target_difficulty}（BASIC 基础记忆 / UNDERSTANDING 理解分析 / APPLICATION 综合应用）
- 自定义要求：{custom_requirements}（可为空）

## 输出

一张卡片 JSON，必须通过 `schemas/v1/card.schema.json`：

- `type`: `question`（问答卡）或 `true_false`（判断题）
- `front`: 卡片正面文本（题目）
- `back`: 卡片背面文本（答案）
- `question` + `answer`: 仅 `question` 卡必填
- `statement` + `answer_boolean` + `explanation`: 仅 `true_false` 卡必填

## 规则

1. 卡片类型自动选择：概念对比/因果适合 `true_false` 时用判断题，其余用问答卡；判断题的 `statement` 表述必须无歧义。
2. 难度匹配目标难度：BASIC 出事实/定义直问；UNDERSTANDING 出对比/推理；APPLICATION 组合多个知识点出场景应用。
3. 内容严格依据原文分片，不得编造；原文不足以支撑时，用该知识点范围内公认事实表述。
4. 只输出 JSON，不输出解释。
```

- [ ] **Step 5: 创建 `agent_evolution/schemas/v1/card.schema.json`**（依据契约 3.9 / PRD 5.8）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Card",
  "type": "object",
  "required": ["type", "front", "back"],
  "properties": {
    "type": { "enum": ["QUESTION", "TRUE_FALSE"] },
    "front": { "type": "string", "minLength": 1 },
    "back": { "type": "string", "minLength": 1 },
    "question": { "type": "string", "minLength": 1 },
    "answer": { "type": "string", "minLength": 1 },
    "statement": { "type": "string", "minLength": 1 },
    "answer_boolean": { "type": "boolean" },
    "explanation": { "type": "string", "minLength": 1 }
  },
  "allOf": [
    {
      "if": { "properties": { "type": { "const": "QUESTION" } }, "required": ["type"] },
      "then": { "required": ["question", "answer"] }
    },
    {
      "if": { "properties": { "type": { "const": "TRUE_FALSE" } }, "required": ["type"] },
      "then": { "required": ["statement", "answer_boolean", "explanation"] }
    }
  ]
}
```

- [ ] **Step 6: 创建 `agent_evolution/rubrics/v1/rubric.md`**（档位表抄录自 PRD 5.9，与契约 3.9 一致）

```markdown
# Rubric 质量评估（v1）

四维评分，每维 0~3 分，总分 0~12。仅观测，不参与入库决策（PRD 5.9）。

| 维度 | 0 分 | 1 分 | 2 分 | 3 分 |
| --- | --- | --- | --- | --- |
| 原文依据 | 无原文依据或与原文冲突 | 原文依据不足 | 基本可由原文支持 | 完整且可追溯 |
| 答案正确性 | 答案错误 | 存在明显问题 | 基本正确 | 完全正确 |
| 难度匹配 | 与目标难度完全不匹配 | 部分匹配 | 基本匹配 | 高度匹配 |
| 学习价值 | 无学习价值 | 学习价值较低 | 具有学习价值 | 具有核心学习价值 |

不设置"表达清晰""原子化"维度；无 PASS/FAIL 状态。评分结果字段：`evidence_score` / `correctness_score` / `difficulty_score` / `learning_value_score` / `rubric_total_score`。
```

- [ ] **Step 7: 创建 `agent_evolution/rubrics/v1/scoring-prompt.md`**

内容要素（依据 PRD 5.9 / 契约 3.9 / O-5）：
- 角色：LLM-as-judge 评分器；输入：卡片内容 + 目标难度 + 原文依据 + rubric 档位表。
- 输出：`{evidence_score, correctness_score, difficulty_score, learning_value_score, rubric_total_score}` 各 0~3，总分自动求和。
- 规则：仅评分不修改卡片；依据不充分时按 1 分处理；只输出 JSON。

首版草稿全文（写入文件）：

```markdown
# Rubric 评分 Prompt（v1）

你是闪卡质量评分器。按档位表对一张卡片评分，不修改卡片内容。

## 输入

- 卡片：{front} / {back}（type={type}）
- 目标难度：{target_difficulty}
- 原文依据：{source_excerpt}

## 评分档位

（见 rubrics/v1/rubric.md：原文依据 / 答案正确性 / 难度匹配 / 学习价值，各 0~3 分）

## 输出

```json
{
  "evidence_score": 0,
  "correctness_score": 0,
  "difficulty_score": 0,
  "learning_value_score": 0,
  "rubric_total_score": 0
}
```

## 规则

1. 各维按档位表给 0~3 分；`rubric_total_score` 为四维之和。
2. 仅评分，不修改卡片文本，不输出解释。
3. 原文依据不足时 `evidence_score` 不超过 1。
```

- [ ] **Step 8: 验证资产完整性**

```bash
cd /home/kbzz1/shanka_backend
python3 -c "
import json, os
m = json.load(open('agent_evolution/manifest.json'))
for kind, items in m.items():
    for name, spec in items.items():
        assert os.path.exists('agent_evolution/' + spec['path']), spec['path']
        assert spec['version'] == 'v1', spec['version']
print('manifest OK: 所有资产存在且 version=v1')
"
python3 -c "import json; json.load(open('agent_evolution/schemas/v1/card.schema.json')); print('card.schema.json OK')"
```

Expected: 两行 OK 输出。

- [ ] **Step 9: 提交**

```bash
git add agent_evolution/
git commit -m "feat(agent-evolution): v1 首版资产（prompt/schema/rubric + manifest + CHANGELOG）"
```

---

### Task 3: project-structure.md 契约更新

**Files:**
- Modify: `docs/Architecture/project-structure.md`

**Interfaces:**
- Consumes: 规格 8 节清单、9.3/9.4；Task 1 已建目录结构。
- Produces: 契约侧对 `agent_evolution/`、tests 四层、工具链、守卫四项的正式定义（Task 8 的 README 引用）。

- [ ] **Step 1: 更新第 1 节仓库总览**——在 `docs/` 与 `main/` 之间插入 `agent_evolution/`：

```text
shanka_backend/
├── docs/                    # 文档（需求 + 设计契约）
│   ├── PRD/
│   │   └── V2.1/prd_v2_1.md
│   └── Architecture/        # 本目录:设计契约(见 README.md)
├── main/                    # 后端实现(按本编排落地)
└── agent_evolution/         # agent 版本化资产:prompt/schema/rubric(目录快照 + manifest)
```

并在依赖方向段落后追加一段：

```markdown
`agent_evolution/` 是 `main/infra/llm/` 的实现资产源（按 manifest 加载），与 `main/` 并行、不反向依赖；资产演进（新版本目录 + 更新 manifest + CHANGELOG）视为技术评审级变更。
```

- [ ] **Step 2: 更新第 3 节 tests 目录**——`tests/` 下补四层结构：

```text
└── tests/
    ├── unit/          # domain、schemas 纯逻辑
    ├── integration/   # services 编排、DB 事务边界
    ├── contract/      # 守卫四项:schemas↔openapi、ORM↔database-design、错误码↔契约 7 章、localization_key↔文案
    └── acceptance/    # AC-01~AC-11 验收映射
```

- [ ] **Step 3: 新增第 5 节"测试策略"**（插在第 4 节之后，原第 5 节顺延为第 6 节）：

```markdown
## 5. 测试策略

- 四层职责：unit（domain/schemas 纯逻辑）、integration（services 编排与 DB 事务边界）、contract（守卫四项）、acceptance（AC-01~AC-11 映射）。
- 命名规范：`test_<模块>_<行为>`。
- 守卫四项（自动化校验一致性红线的验证手段）：
  1. `app/schemas` ↔ `openapi.yaml`
  2. `infra/db` ORM ↔ `database-design.md`
  3. 错误码清单 ↔ `structure-contract.md` 第 7 章
  4. `localization_key` ↔ 文案资产清单
- 幂等同事务、级联删除、resume 并发等易碎行为必须出现在 integration 层（对应 database-design 3 的事务边界）。
- 验收回归（P3-2）：每条 AC-01~AC-11 有对应 acceptance 用例。
```

- [ ] **Step 4: 新增第 6 节"开发工具链"**（插在测试策略之后，原第 6 节顺延为第 7 节）：

```markdown
## 6. 开发工具链

- 依赖唯一事实源：`main/pyproject.toml`（声明 + ruff/mypy 配置）；依赖锁定文件在 P0-1 首次安装时生成。
- pre-commit 本地钩子：format（ruff-format）→ lint（ruff）→ type-check（mypy strict）。
- CI 在远端仓库就绪后补建；本期不建。
- 配置分层（P0-1 要求）：pydantic-settings 单层配置类；默认值进代码；密钥/令牌走环境变量；敏感项清单文档化；禁止散落硬编码。
```

- [ ] **Step 5: 验证与提交**

```bash
cd /home/kbzz1/shanka_backend
grep -c "agent_evolution" docs/Architecture/project-structure.md   # ≥ 2
grep -c "守卫四项" docs/Architecture/project-structure.md          # ≥ 1
grep -c "## 6. 开发工具链" docs/Architecture/project-structure.md # = 1
git add docs/Architecture/project-structure.md
git commit -m "docs(contract): project-structure 更新（agent_evolution/tests 四层/测试策略/工具链）"
```

---

### Task 4: structure-contract.md 契约更新

**Files:**
- Modify: `docs/Architecture/structure-contract.md`

**Interfaces:**
- Consumes: 规格 7 节（O-1~O-6）、9.1。
- Produces: 运行可观测性、6.10 聚合观测接口、任务架构定式的契约定义（Task 6 openapi 与之一致）。

- [ ] **Step 1: 在 4.3 之后新增 4.4"任务执行架构定式"**：

```markdown
### 4.4 任务执行架构定式

- **进程内调度器**：PDF 解析、知识点规划、分批生成由 API 进程内后台循环扫描 PENDING 任务/批次执行；任务/批次状态与游标存 DB（**DB 即状态**），不引入外部任务队列（Celery/RQ/Redis）。
- **多实例演进**：孤儿 RUNNING 心跳恢复（30 分钟）+ DB 条件更新抢占已支持多 worker；未来多实例仅增加 DB 轮询调度，业务逻辑不变。
- 禁止以性能为由提前引入任务队列。
```

- [ ] **Step 2: 在 6.9 之后新增 6.10 聚合观测接口**：

```markdown
### 6.10 质量聚合观测（O-4，审核设计补全）

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/observability/quality-summary?group_by=model\|pdf\|difficulty&days=30` | 跨任务质量聚合:Rubric 各维平均分、覆盖/重复率均值、任务完成率、成本汇总;按 group_by 分组 | - |

- 隔离口径：按当前 `device_id` 聚合（与业务数据同隔离）；跨设备聚合留给未来运营后台。
- 成本汇总（O-6）：按"价格配置常量"换算 `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens` 为估算金额，hit/miss/output 分开计价；价格常量取 DeepSeek 官方定价、标注生效日期，不固化进 DB。
- `/healthz`（存活）、`/readyz`（就绪:DB 连接 + 存储可写，失败 503）、`/metrics`（Prometheus 文本）为运行观测基础端点，**豁免 X-Device-ID 鉴权**（探针/采集器无设备上下文）。
```

- [ ] **Step 3: 新增第 8 章"运行可观测性"**（原第 8 章"与 PRD 的对照"顺延为第 9 章）：

```markdown
## 8. 运行可观测性（观测范围仅 DeepSeek API）

### 8.1 结构化日志（O-1）

- JSON 单行格式；字段:`timestamp`(ISO 8601 UTC) / `level` / `request_id` / `device_id` / `task_id` / `batch_id` / `error_code` / `message`。
- 级别规范:INFO(请求进出、批处理完成)、WARN(重试、限流触发)、ERROR(异常 + error_code)。
- 贯穿机制:中间件生成 `request_id` 贯穿全链路;后台批处理以 `task_id` + `batch_id` 关联。
- 红线保留:1.5/7.1 的 API Key、完整 PDF 内容、完整 Prompt 不落日志。

### 8.2 健康检查（O-2）

`GET /healthz` 存活探针;`GET /readyz` 就绪探针(DB 连接 + 存储可写,失败 503);豁免 X-Device-ID 鉴权。

### 8.3 指标（O-3）

`GET /metrics`(Prometheus 文本格式),豁免 X-Device-ID 鉴权,生产子域名默认不暴露:

| 指标 | 类型 | labels |
| --- | --- | --- |
| `generation_tasks_total` | counter | result(COMPLETED/FAILED/CANCELLED) |
| `generation_tasks_duration_seconds` | histogram | - |
| `batch_retry_total` | counter | - |
| `rate_limit_hit_total` | counter | scope(device/ip/api_key/samples/pdf) |
| `llm_requests_total` | counter | model(DeepSeek 模型族)/http_status |
| `llm_request_duration_seconds` | histogram | model |
| `llm_tokens_total` | counter | kind(cache_hit/cache_miss/output) |
| `http_requests_total` | counter | method/path/status |
| `http_request_duration_seconds` | histogram | - |

### 8.4 成本观测（O-6）

- 原始 token 数据(`cache_hit_tokens` / `cache_miss_tokens` / `output_tokens`)落 Batch 表,不变。
- 估算成本在聚合时按"价格配置常量"换算;常量取 DeepSeek 官方定价、标注生效日期;价格调整只改配置,不动历史数据。
- 出口:8.3 `llm_tokens_total` 与 6.10 聚合接口的成本汇总。

### 8.5 评估骨架（O-5）

- Rubric 评分执行者:LLM-as-judge;评分 prompt 资产: `agent_evolution/rubrics/v1/scoring-prompt.md`。
- `rubric_version` / `prompt_version` / `schema_version` 字段值 = `agent_evolution/manifest.json` 中对应 version。
- 评分请求记录:prompt 版本 + 输入摘要 + 输出分;不落完整 prompt。
```

- [ ] **Step 4: 更新原第 8 章对照表（现为第 9 章）**——追加一行：

```markdown
| 8 运行可观测性 / 6.10 聚合观测 | PRD 8 核心指标 / FR-10 / FR-11 | 新增(设计规格 6422765) |
```

- [ ] **Step 5: 验证与提交**

```bash
cd /home/kbzz1/shanka_backend
grep -c "## 8. 运行可观测性" docs/Architecture/structure-contract.md     # = 1
grep -c "### 6.10" docs/Architecture/structure-contract.md               # = 1
grep -c "### 4.4 任务执行架构定式" docs/Architecture/structure-contract.md # = 1
git add docs/Architecture/structure-contract.md
git commit -m "docs(contract): structure-contract 新增运行可观测性/6.10 聚合观测/任务架构定式"
```

---

### Task 5: database-design.md 契约更新

**Files:**
- Modify: `docs/Architecture/database-design.md`

**Interfaces:**
- Consumes: 规格 9.2。
- Produces: 演进路径与迁移工具选型的契约定义（Task 8 引用）。

- [ ] **Step 1: 在末尾新增第 7 章"演进路径"**（在现有第 6 章之后）：

```markdown
## 7. 演进路径

### 7.1 账号体系（未来）

- 新增 `users` 表;`devices` 增加可空外键列 `user_id`(先 NULL 后回填)。**不重构 devices 主键**,匿名设备 ID 体系维持为兼容层。
- 数据迁移:按绑定关系批量回填 `user_id` 后加 NOT NULL;业务表隔离键仍为 `device_id`。

### 7.2 新卡类型（未来）

- 沿用 D-01 模式:专用列 + `front`/`back` 通用渲染。
- 类型数可控(≤5)时继续用专用列;字段高度异构或继续膨胀时,评估 JSON 扩展列方案。
- 所有结构变更走迁移工具。

### 7.3 迁移工具选型

- 选型:**Alembic**(SQLAlchemy 官方迁移工具);P0-2 引入并生成首个迁移。
- 迁移纪律:与 ORM 模型同 PR 提交;破坏性变更需同步更新 database-design 与契约。
```

- [ ] **Step 2: 验证与提交**

```bash
cd /home/kbzz1/shanka_backend
grep -c "## 7. 演进路径" docs/Architecture/database-design.md # = 1
grep -c "Alembic" docs/Architecture/database-design.md         # ≥ 1
git add docs/Architecture/database-design.md
git commit -m "docs(contract): database-design 新增演进路径章节与 Alembic 选型"
```

---

### Task 6: openapi.yaml 契约更新

**Files:**
- Modify: `docs/Architecture/openapi.yaml`

**Interfaces:**
- Consumes: Task 4 的 6.10/8.2/8.3 定义。
- Produces: 机器可读接口定义与 structure-contract 一致（守卫 1 的依据）。

- [ ] **Step 1: 在 paths 末尾（`/v1/tasks/{task_id}/batches` 之后）追加三个路径**：

```yaml
  /healthz:
    get:
      summary: 存活探针
      tags: [observability]
      security: []
      responses:
        "200":
          description: 进程存活
  /readyz:
    get:
      summary: 就绪探针
      tags: [observability]
      security: []
      responses:
        "200":
          description: DB 连接与存储可写
        "503":
          description: 依赖不可用
  /v1/observability/quality-summary:
    get:
      summary: 跨任务质量聚合观测（契约 6.10）
      tags: [observability]
      parameters:
        - name: group_by
          in: query
          required: false
          schema:
            type: string
            enum: [model, pdf, difficulty]
        - name: days
          in: query
          required: false
          schema:
            type: integer
            default: 30
      responses:
        "200":
          description: 质量聚合结果（Rubric 均分 / 覆盖重复率 / 完成率 / 成本汇总）
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/QualitySummary"
```

> 说明：`security: []` 表示豁免 X-Device-ID 鉴权（/healthz、/readyz 为探针端点；与结构契约 8.2 一致）。`/metrics` 不进 openapi（非业务 API）。

- [ ] **Step 2: 在 components.schemas 末尾追加 `QualitySummary`**：

```yaml
    QualitySummary:
      type: object
      properties:
        group_by:
          type: string
        groups:
          type: array
          items:
            type: object
            properties:
              key:
                type: string
              card_count:
                type: integer
              evidence_avg / correctness_avg / difficulty_avg / learning_value_avg:
                type: number
                format: float
              coverage_avg / duplicate_avg:
                type: number
                format: float
              task_completion_rate:
                type: number
                format: float
              cost_estimate:
                type: object
                properties:
                  cache_hit: { type: number, description: 估算金额(元) }
                  cache_miss: { type: number }
                  output: { type: number }
                  total: { type: number }
        days:
          type: integer
        generated_at:
          type: string
```

> 注：YAML 中 `evidence_avg / correctness_avg / ...` 一行多 key 为简写技巧，实际写入时逐项展开为独立键（每项 `type: number`）。

- [ ] **Step 3: 验证与提交**

```bash
cd /home/kbzz1/shanka_backend
python3 -c "import yaml; d = yaml.safe_load(open('docs/Architecture/openapi.yaml')); assert '/healthz' in d['paths']; assert '/v1/observability/quality-summary' in d['paths']; assert 'QualitySummary' in d['components']['schemas']; print('openapi OK')"
git add docs/Architecture/openapi.yaml
git commit -m "docs(contract): openapi 新增 healthz/readyz/quality-summary"
```

---

### Task 7: deployment.md 部署设计文档

**Files:**
- Create: `docs/Architecture/deployment.md`

**Interfaces:**
- Consumes: 规格第 6 节（Tunnel 架构/子域名/接入要点/延迟阶梯）。
- Produces: P3-4 实施依据；Task 8 的 README 文档清单引用。

- [ ] **Step 1: 创建 `docs/Architecture/deployment.md`**，内容结构如下（完整撰写，数据源为规格第 6 节）：

```markdown
# 部署设计（Cloudflare Tunnel）v2.1

## 1. 现状与目标
域名已在 Cloudflare 管理;无公网 IP / 无服务器;目标:Android 前端经 HTTPS 访问后端 API。
**实施时机**:实际接入(P3-4,最后阶段);本文档为设计定稿。

## 2. 架构
(规格 6.1 架构图:Android → api.<domain>(CF 边缘) → Tunnel → cloudflared(WSL2) → FastAPI)

## 3. 子域名规划
| 子域名 | 用途 | Tunnel 路由 |
| api.<domain> | 生产 API(App 连接) | localhost:<port>(默认 8000,可配置) |
| dev.api.<domain> | 开发联调 | 同上或独立端口 |

## 4. 接入步骤(P3-4 执行)
1. 端口检测:启动前检查占用,被占用则换端口并同步 Tunnel 路由;FastAPI 监听端口为配置项(环境变量覆盖)。
2. Cloudflare Zero Trust → Networks → Tunnels → 创建命名隧道(如 `shanka-api`),记录 Tunnel Token。
3. WSL2 安装 cloudflared(`curl -L https://pkg.cloudflare.com/cloudflare-main.gpg ...`),常驻运行(systemd / nohup)。
4. 公共主机名配置:`api.<domain>` → `localhost:<port>`。
5. TLS:边缘自动 HTTPS;回源走 Tunnel 内部加密,不暴露端口。
6. 可选加固:WAF 自定义规则(限流);`/metrics` 只走 dev 子域名或加 Access。

## 5. 与契约衔接
- 契约 1.7(HTTPS):边缘层 TLS 终止,天然满足。
- 契约 1.6(应用层限流)为兜底;CF 边缘限流为外层防线,两层互补。
- /healthz、/readyz 供 Tunnel/监控探活。

## 6. 大陆访问延迟:阶梯决策
| 阶段 | 方案 | 成本 |
| MVP 开发联调 | Tunnel + CF 边缘,真机实测(移动网络通常走香港节点,100~250ms) | 零 |
| 实测不可接受 | 灰云 + 香港 VPS:CF 只做 DNS(灰云),Nginx + Let's Encrypt(CF DNS-01 challenge);自管反代/证书/防火墙 | 约 $5/月 |
| 真实大陆用户 | 国内云 + ICP 备案(服务器 + 域名双备案) | 最高 |

- 灰云与 Tunnel 不兼容;升级 = 部署 VPS → 改 DNS(橙云变灰云)→ 迁移证书;代码层不受影响。
- 决策依据:低频短请求 + 前端 Room 缓存,100~250ms 无感知差异;MVP 不为规模问题提前优化。

## 7. 运维注意
- cloudflared 常驻与自启;Tunnel Token 为敏感凭据,不入仓库。
- 延迟/连通性实测记录处(迁移决策输入)。
```

- [ ] **Step 2: 验证与提交**

```bash
cd /home/kbzz1/shanka_backend
grep -c "## 6. 大陆访问延迟" docs/Architecture/deployment.md # = 1
test -f docs/Architecture/deployment.md && echo "deployment.md 存在"
git add docs/Architecture/deployment.md
git commit -m "docs(deploy): Cloudflare Tunnel 部署设计定稿（P3-4 依据）"
```

---

### Task 8: README + Progress 收尾与最终验证

**Files:**
- Modify: `docs/Architecture/README.md`
- Modify: `docs/Progress.md`

**Interfaces:**
- Consumes: Task 3~7 全部契约更新。
- Produces: 文档入口一致性（防漂移规则 5）。

- [ ] **Step 1: 更新 `docs/Architecture/README.md` 文档关系表**——追加两行：

```markdown
| [deployment.md](deployment.md) | 部署设计:Cloudflare Tunnel 接入与迁移阶梯 | 部署(P3-4) |
| `agent_evolution/`(仓库根目录) | agent 版本化资产(prompt/schema/rubric),manifest 为运行时加载入口 | 后端 infra/llm |
```

- [ ] **Step 2: 更新 `docs/Architecture/README.md` 防漂移规则**——在规则 5 后追加规则 6：

```markdown
6. **资产权威**:`agent_evolution/manifest.json` 为 prompt/schema/rubric 唯一版本入口;`structure-contract.md` 中的 `prompt_version` / `schema_version` / `rubric_version` 必须与 manifest 一致。
```

- [ ] **Step 3: 更新 `docs/Progress.md`**

- P0-0 行任务描述补全:"项目骨架:目录与模块空文件编排(对齐 project-structure.md,含 tests/ 四层目录、pyproject/pre-commit 工具链)",状态标 ✅。
- 第 4 节审核与文档修订表追加一行:

```markdown
| 契约文档更新(project-structure/structure-contract/database-design/openapi/deployment/README) | ✅ | 2026-08-10 实施完成(P0-0 产物) |
```

- [ ] **Step 4: 最终一致性验证**

```bash
cd /home/kbzz1/shanka_backend
# 1. 全部新文件存在
test -f main/pyproject.toml && test -f agent_evolution/manifest.json && test -f docs/Architecture/deployment.md && echo "文件齐全"
# 2. 交叉引用一致:deployment 出现在 README 文档清单
grep -c "deployment.md" docs/Architecture/README.md            # ≥ 1
# 3. manifest 与契约字段一致性(8 章引用)
grep -c "manifest" docs/Architecture/README.md                 # ≥ 1
# 4. git 状态干净
git status --short
```

Expected: "文件齐全" + grep 计数符合 + git status 仅显示上述已提交内容（或干净）。

- [ ] **Step 5: 提交**

```bash
git add docs/Architecture/README.md docs/Progress.md
git commit -m "docs: README 文档清单/防漂移规则更新 + Progress P0-0 完成"
```

---

## Self-Review 记录

**Spec 覆盖核对：**
- 规格 3/4（布局/骨架）→ Task 1 ✅
- 规格 5（agent_evolution v1 首版 + manifest + CHANGELOG）→ Task 2 ✅
- 规格 8 文档清单（project-structure/structure-contract/database-design/openapi/deployment/README/Progress）→ Task 3~8 ✅
- 规格 7（O-1~O-6 入契约）→ Task 4（日志/健康检查/指标/成本/评估骨架）+ Task 6（接口）✅
- 规格 9.1（任务架构定式）→ Task 4 Step 1 ✅
- 规格 9.2（数据演进路径 + Alembic）→ Task 5 ✅
- 规格 9.3/9.4（工具链/守卫四项）→ Task 1（pyproject/pre-commit）+ Task 3（测试策略/工具链章节）✅
- 规格 9.5（配置分层）→ Task 3 Step 4（工具链章节内）✅
- 规格 6（Tunnel 设计，实操推后）→ Task 7 ✅
- 规格 2（决策表）→ 各任务 Global Constraints 隐含 ✅

**无占位符检查：** 所有文件内容均给出完整文本或逐项清单；pyproject/pre-commit/资产/契约章节均为可复制内容。唯一"推迟"事项（依赖锁定文件、Tunnel 实操、CI）均有明确归属任务（P0-1/P3-4/远端就绪后），非 TBD。

**类型/命名一致性：** manifest 键名（planner/generator/card/main）在 Task 2 与规格 5.2 一致；`agent_evolution/` 全计划统一；契约章节编号顺延规则（Task 3 第 4→5→6 节、Task 4 第 8 章）在各步骤内明确。
