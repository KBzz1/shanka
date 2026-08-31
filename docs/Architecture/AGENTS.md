# AGENTS.md

本目录是闪卡 App V2.5 的设计契约集合，前后端实现的权威依据。需求上游见 [PRD V2.5](../PRD/V2.5/prd_v2_5.md)（增量继承历史版本）。

## 文档关系

| 文档 | 内容 | 主要消费方 |
| --- | --- | --- |
| project-structure.md | 仓库布局、后端分层、文档层级与依赖规则 | 后端、文档维护 |
| structure-contract.md | 总则（鉴权/幂等/时间/错误码）、资源模型、状态机、FSRS-6 排程、接口清单 | 前后端 |
| openapi.yaml | OpenAPI 3.1 接口机器契约：路径、请求/响应 schema | 前后端（代码生成与校验） |
| database-design.md | 表、列、索引、唯一约束、幂等键 | 后端 |
| deployment.md | Cloudflare Tunnel 接入与迁移阶梯 | 部署（P3-4） |
| `agent_evolution/`（仓库根目录） | agent 版本化资产（prompt/schema/rubric），manifest 为运行时加载入口 | 后端 infra/llm |

## 单一事实来源（防漂移规则）

1. **需求权威**：`docs/PRD/V2.5/prd_v2_5.md` 及同目录模块 PRD（增量继承历史版本）。契约与 PRD 冲突时，以当前版本 PRD 已确认决策为准，并同步修订另一侧。
2. **字段权威**：`structure-contract.md` 第 3 章资源模型。`openapi.yaml` 的 schema 与 `database-design.md` 的表结构均从资源模型派生，**禁止另立字段定义**。
3. **接口机器权威**：`openapi.yaml`（路径、请求/响应结构）。`structure-contract.md` 负责行为契约（状态机、幂等、排程、错误码）。
4. **持久化权威**：`database-design.md`，每个表必须能回溯到资源模型中的对应资源。
5. 修改任一文档时，检查其下游文档是否需要同步（资源模型变更 → openapi schema + 数据库表）。
6. **资产权威**：`agent_evolution/manifest.json` 为 prompt/schema/rubric 唯一版本入口；`structure-contract.md` 中的 `prompt_version` / `schema_version` / `rubric_version` 必须与 manifest 一致。

## 版本管理

- 文档与 PRD 版本对齐：V2.5 对应 PRD V2.5（增量继承历史版本）。
- 破坏性变更（删除字段、修改语义）需同步更新 PRD 与验收标准；兼容性变更（新增可选字段）只需更新契约。

## 已确认决策（2026-08-10）

| 编号 | 事项 | 确认值 | 影响 |
| --- | --- | --- | --- |
| C-01 | FSRS 学习阶段步进 `learning_steps` | `(10m, 10m, 1d)`（R-13：py-fsrs 语义下 3 步复现 5.2 表；C-01 意图=新卡 10 分钟后复现,次日复现后毕业） | 新卡学习阶段间隔 |
| C-02 | FSRS `enable_fuzzing` | `false`（确定性，便于幂等与测试） | 间隔确定性 |
| C-03 | 掌握判定阈值 | `state == REVIEW` 且 `stability >= 21` 天（Anki 成熟口径） | 牌组进度与看板 |
| C-04 | 幂等重放响应 | 重复请求返回首次成功结果（200/201 + 原响应体），不返回 409 | 全写操作 |
| C-05 | 单卡重写替换方式 | 原地替换（同 `card_id`），复习状态重置，新 `generation_item_id` | 单卡重写（FR-13） |
| C-06 | 未到期评级宽容 | 评级接口允许对任意状态卡片提交，服务端按 FSRS 正常排程 | 复习（FR-15） |
| C-07 | FSRS 其余参数 | `desired_retention=0.9`、`relearning_steps=(10m,)`、`maximum_interval=36500`，取自 FSRS-6 默认/Anki 惯例 | 复习（FR-15） |
