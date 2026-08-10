# 文件夹结构编排(v2.1)

## 1. 仓库总览

```text
shanka_backend/
├── docs/                    # 文档(需求 + 设计契约)
│   ├── PRD/
│   │   └── V2.1/prd_v2_1.md
│   └── Architecture/        # 本目录:设计契约(见 AGENTS.md)
├── main/                    # 后端实现(按本编排落地)
└── agent_evolution/         # agent 版本化资产:prompt/schema/rubric(目录快照 + manifest)
```

**依赖方向**:`docs/PRD → docs/Architecture → main/`,单向向下。实现不得反向驱动契约;契约变更必须走评审。

`agent_evolution/` 是 `main/infra/llm/` 的实现资产源（按 manifest 加载），与 `main/` 并行、不反向依赖；资产演进（新版本目录 + 更新 manifest + CHANGELOG）视为技术评审级变更。

## 2. 文档层级

| 层级 | 位置 | 回答的问题 | 变更触发 |
| --- | --- | --- | --- |
| 需求 | `docs/PRD/V2.1/` | 做什么、为什么 | 产品决策 |
| 契约 | `docs/Architecture/` | 前后端如何对接、数据长什么样 | 技术评审 |
| 实现 | `main/` | 具体代码 | 日常开发 |

文档命名:小写 + 连字符(`structure-contract.md`、`database-design.md`);按 PRD 版本对齐版本号,不引入多余层级。

## 3. 后端代码结构(`main/`)

采用**契约驱动分层**:每一层都对应契约中的一个明确概念,保证"契约 → 代码"可追溯。

```text
main/
├── app/                         # 应用层:HTTP 出入口
│   ├── api/                     # 路由与 handler,按契约接口分组
│   │   ├── pdfs.py
│   │   ├── api_key.py
│   │   ├── samples.py
│   │   ├── tasks.py
│   │   ├── decks.py
│   │   ├── cards.py
│   │   ├── review.py
│   │   └── stats.py
│   ├── middleware/              # X-Device-ID 鉴权、Idempotency-Key 幂等、统一错误响应
│   ├── schemas/                 # 请求/响应模型 —— 必须与 openapi.yaml 一致
│   └── main.py                  # 应用装配
├── domain/                      # 领域模型 —— 对应 structure-contract.md 第 3 章资源模型
│   ├── pdf_file.py  chapter.py  task.py  knowledge_point.py  batch.py
│   ├── deck.py  card.py  review_state.py  review_event.py  api_key.py
│   └── enums.py                 # 全部枚举:状态、评级、类型、来源
├── services/                    # 用例层:编排领域逻辑
│   ├── pdf/                     # 上传、解析、章节提取(目录)
│   ├── generation/              # 样卡、知识点规划、分批生成、Schema 校验、Rubric、单卡重写
│   ├── scheduling/              # FSRS-6(py-fsrs)封装:评级 → 排程更新
│   ├── decks/                   # 牌组、卡片追加、导入
│   └── stats/                   # 看板聚合(周活动、正确率、连续天数)
├── infra/                       # 基础设施
│   ├── db/                      # ORM 模型与迁移 —— 必须与 database-design.md 一致
│   ├── storage/                 # PDF 文件存储
│   └── llm/                     # DeepSeek 调用、Prompt 组装、Prompt Cache 记录(不落 Key)
└── tests/
    ├── unit/          # domain、schemas 纯逻辑
    ├── integration/   # services 编排、DB 事务边界
    ├── contract/      # 守卫四项:schemas↔openapi、ORM↔database-design、错误码↔契约 7 章、localization_key↔文案
    └── acceptance/    # AC-01~AC-11 验收映射
```

## 4. 分层依赖规则

- `domain/` 不依赖任何其他包,是纯数据结构与枚举。
- `app/`、`services/`、`infra/` 可依赖 `domain/`,但相互之间按 `app → services → infra` 单向依赖。
- `app/schemas/`(接口模型)与 `domain/`(领域模型)允许结构相同但职责分离:前者是契约视图,后者是业务对象;禁止在 handler 中直接暴露 ORM 对象。

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

## 6. 开发工具链

- 依赖唯一事实源：`main/pyproject.toml`（声明 + ruff/mypy 配置）；依赖锁定文件在 P0-1 首次安装时生成。
- pre-commit 本地钩子：format（ruff-format）→ lint（ruff）→ type-check（mypy strict）。
- CI 在远端仓库就绪后补建；本期不建。
- 配置分层（P0-1 要求）：pydantic-settings 单层配置类；默认值进代码；密钥/令牌走环境变量；敏感项清单文档化；禁止散落硬编码。

## 7. 一致性红线(评审检查点)

1. `app/schemas/` 字段 ↔ `openapi.yaml` ↔ `structure-contract.md` 资源模型,三处一致。
2. `infra/db/` ORM ↔ `database-design.md` 表结构一致。
3. 幂等键、设备 ID 头、错误码格式的实现在 `app/middleware/` 统一,禁止散落各处。
4. API Key 只出现在 `infra/llm/` 的调用路径中,任何日志、响应、任务明细不得引用其明文;通用请求日志对 `PUT /api-key` 请求体强制掩码;llm 层异常统一脱敏为 `API_KEY_*` / `GENERATION_FAILED` 错误码,日志仅记录 request_id、上游状态码、异常类型(审核修复)。
