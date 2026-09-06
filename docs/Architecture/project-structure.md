# 文件夹结构编排(v2.5)

> 本文是仓库布局与后端分层的**详细编排**；根 `AGENTS.md` 的「仓库布局」是速览。两处以本文为准（根 AGENTS.md 概述不展开目录细节）；目录实际增删时同步本文。

## 1. 仓库总览

```text
shanka_backend/
├── docs/                    # 文档(需求 + 设计契约 + 前端对接 + 工作流产物)
│   ├── PRD/V2.5/            # 需求权威(总 PRD + 7 模块 PRD;V2.1~V2.4 为增量继承的历史链)
│   ├── Architecture/        # 设计契约(见本目录 AGENTS.md 的单一事实来源表)
│   ├── frontend/            # 前端对接文档(backend-integration / local-dev / offline-data-layer)
│   ├── superpowers/         # 工作流产物(plans/specs/handoffs,过程记录)
│   └── Progress.md          # V2.5 执行地图(唯一进度事实源)
├── main/                    # 后端实现(按本编排落地)
├── frontend/                # Android 客户端 subtree(Front/ Gradle 工程 + 前端侧文档)
├── agent_evolution/         # agent 版本化资产:prompt/schema/rubric(目录快照 + manifest)
├── test-platform/           # 自动化联调验证平台(零依赖纯 stdlib,独立于 main 环境)
├── scripts/                 # run.sh / stop.sh(启停)/ gen_sample_cards.py(样卡生成演示)
├── res/                     # 样书 PDF 夹具(只读引用)
└── releases/                # 签名 APK 与发布产物(git 忽略构建物)
```

**依赖方向**:`docs/PRD → docs/Architecture → main/ + frontend/Front`,单向向下。实现不得反向驱动契约;契约变更必须走评审。

`agent_evolution/` 是 `main/infra/llm/` 的实现资产源（按 manifest 加载），与 `main/` 并行、不反向依赖；资产演进（新版本目录 + 更新 manifest + CHANGELOG）视为技术评审级变更。

## 2. 文档层级

| 层级 | 位置 | 回答的问题 | 变更触发 |
| --- | --- | --- | --- |
| 需求 | `docs/PRD/V2.5/` | 做什么、为什么 | 产品决策 |
| 契约 | `docs/Architecture/` | 前后端如何对接、数据长什么样 | 技术评审 |
| 实现 | `main/`、`frontend/Front/` | 具体代码 | 日常开发 |

文档命名:小写 + 连字符(`structure-contract.md`、`database-design.md`);按 PRD 版本对齐版本号,不引入多余层级。

## 3. 后端代码结构(`main/`)

采用**契约驱动分层**:每一层都对应契约中的一个明确概念,保证"契约 → 代码"可追溯。

```text
main/
├── app/                         # 应用层:HTTP 出入口
│   ├── api/                     # 路由与 handler,按契约接口分组
│   │   ├── auth.py  preferences.py  projects.py  study.py        # V2.5 账号/偏好/项目/今日计划
│   │   ├── api_key.py  tasks.py                                   # API Key/制卡任务
│   │   ├── decks.py  cards.py  review.py  stats.py                # 牌组卡片/复习/看板
│   │   └── metrics.py  observability.py  probes.py                # 观测与探针
│   ├── middleware/              # Bearer 认证、Idempotency-Key 幂等、IP 令牌桶限流、
│   │                            # 统一错误响应、Request-ID、日志与指标(装配顺序见 main.py)
│   ├── schemas/                 # 请求/响应模型 —— 必须与 openapi.yaml 一致
│   └── main.py                  # 应用装配
├── domain/                      # 领域模型 —— 对应 structure-contract.md 第 3 章资源模型
│   ├── pdf_file.py  chapter.py  project.py  task.py  knowledge_point.py  batch.py
│   ├── deck.py  card.py  deletion_batch.py  rewrite_preview.py
│   ├── review_state.py  review_event.py  preferences.py
│   ├── api_key.py  auth.py
│   └── enums.py                 # 全部枚举:状态、评级、类型、来源
├── services/                    # 用例层:编排领域逻辑
│   ├── pdf/                     # 上传、解析、章节提取(目录)
│   ├── generation/              # 规划、分批生成、Schema 校验、Rubric、评分、样卡、配额与成本
│   ├── scheduling/              # FSRS-6(py-fsrs)封装:评级 → 排程更新
│   ├── projects/  study/        # 学习项目、今日学习计划(V2.5)
│   ├── decks/  cards/  deletion/  # 牌组、卡片、删除批次(撤销窗口)
│   ├── review/  stats/  progress/  preferences/  # 复习、看板聚合、进度投影、账号偏好
│   ├── auth/  api_key/          # 账号会话、DeepSeek Key 校验与保管
│   └── tasks/                   # 任务执行器:租约/心跳、状态机、操作编排
├── infra/                       # 基础设施
│   ├── db/                      # ORM 模型与迁移(main/migrations/ 为 Alembic 迁移)
│   ├── storage/                 # PDF 文件存储
│   └── llm/                     # DeepSeek 调用、Prompt 组装(按 agent_evolution manifest 加载;不落 Key)
└── tests/
    ├── unit/          # domain、schemas 纯逻辑
    ├── services/      # 服务层纯逻辑(无 DB 编排)
    ├── integration/   # services 编排、DB 事务边界
    ├── contract/      # 守卫四项:schemas↔openapi、ORM↔database-design、错误码↔契约 7 章、localization_key↔文案
    ├── acceptance/    # 验收映射(PRD AC)
    ├── app/  infra/   # HTTP 装配与基础设施单测
    └── live/          # 受控真实 DeepSeek 验证(显式触发,见 tests/live/README.md)
```

## 4. 分层依赖规则

- `domain/` 不依赖任何其他包,是纯数据结构与枚举。
- `app/`、`services/`、`infra/` 可依赖 `domain/`,但相互之间按 `app → services → infra` 单向依赖。
- `app/schemas/`(接口模型)与 `domain/`(领域模型)允许结构相同但职责分离:前者是契约视图,后者是业务对象;禁止在 handler 中直接暴露 ORM 对象。

## 5. 测试策略

- 分层职责：unit（domain/schemas 纯逻辑）、services（服务层纯逻辑）、integration（services 编排与 DB 事务边界）、contract（守卫四项）、acceptance（验收映射）、app/infra（装配与基础设施）、live（受控真实 LLM,显式触发）。
- 命名规范：`test_<模块>_<行为>`。
- 守卫四项（自动化校验一致性红线的验证手段）：
  1. `app/schemas` ↔ `openapi.yaml`
  2. `infra/db` ORM ↔ `database-design.md`
  3. 错误码清单 ↔ `structure-contract.md` 第 7 章
  4. `localization_key` ↔ 文案资产清单
- 幂等同事务、级联删除、任务并发/租约等易碎行为必须出现在 integration 层（对应 database-design 的事务边界）。
- 验收回归：PRD 验收标准有对应 acceptance 用例。
- 工具链与运行方式见根 `AGENTS.md`「工具链」（Conda 环境 `shanka-backend`;`cd main && conda run -n shanka-backend python -m pytest`）。

## 6. 开发工具链

- 依赖唯一事实源：`main/pyproject.toml`（声明 + ruff line-length 100 / mypy strict 配置）。
- pre-commit 本地钩子：format（ruff-format）→ lint（ruff）→ type-check（mypy strict）。
- 配置分层：pydantic-settings 单层配置类；默认值进代码；密钥/令牌走环境变量；禁止散落硬编码。

## 7. 一致性红线(评审检查点)

1. `app/schemas/` 字段 ↔ `openapi.yaml` ↔ `structure-contract.md` 资源模型,三处一致。
2. `infra/db/` ORM ↔ `database-design.md` 表结构一致。
3. 幂等键、Bearer 认证、错误码格式的实现在 `app/middleware/` 统一,禁止散落各处。
4. API Key 只出现在 `infra/llm/` 的调用路径中,任何日志、响应、任务明细不得引用其明文;通用请求日志对 `PUT /api-key` 请求体强制掩码;llm 层异常统一脱敏为 `API_KEY_*` / `GENERATION_FAILED` 错误码,日志仅记录 request_id、上游状态码、异常类型。
