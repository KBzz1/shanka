# 文件夹结构编排(v2.1)

## 1. 仓库总览

```text
shanka_backend/
├── docs/                    # 文档(需求 + 设计契约)
│   ├── PRD/
│   │   └── V2.1/prd_v2_1.md
│   └── Architecture/        # 本目录:设计契约(见 README.md)
└── main/                    # 后端实现(当前为空,按本编排落地)
```

**依赖方向**:`docs/PRD → docs/Architecture → main/`,单向向下。实现不得反向驱动契约;契约变更必须走评审。

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
```

## 4. 分层依赖规则

- `domain/` 不依赖任何其他包,是纯数据结构与枚举。
- `app/`、`services/`、`infra/` 可依赖 `domain/`,但相互之间按 `app → services → infra` 单向依赖。
- `app/schemas/`(接口模型)与 `domain/`(领域模型)允许结构相同但职责分离:前者是契约视图,后者是业务对象;禁止在 handler 中直接暴露 ORM 对象。

## 5. 一致性红线(评审检查点)

1. `app/schemas/` 字段 ↔ `openapi.yaml` ↔ `structure-contract.md` 资源模型,三处一致。
2. `infra/db/` ORM ↔ `database-design.md` 表结构一致。
3. 幂等键、设备 ID 头、错误码格式的实现在 `app/middleware/` 统一,禁止散落各处。
4. API Key 只出现在 `infra/llm/` 的调用路径中,任何日志、响应、任务明细不得引用其明文。
