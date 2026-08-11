# Agent 成本/用量观测能力层 + 任务价格预估接口设计（2026-08-12）

## 1. 背景与目标

- 用户自持 DeepSeek API Key（app 的 LLM 成本由用户自己的 Key 承担），生成任务前需要知道「这个任务预计花多少钱」。
- 现有可观测性零散资产：`services/generation/cost.py`（价格档位 + 事后核算）、`services/generation/llm_metrics.py`（8.3 指标上报）、`GET /observability/quality-summary`（6.10 聚合，含事后成本汇总）、Batch 表 usage 观测列。
- 本次方向（用户决策）：**不做 Mock AI / staging fake，全量走真实 API Key**；价格预估是「Agent 成本/用量观测能力」的一个消费点，而非孤立接口——先有可演进的能力层，接口只是顺带调用。
- 目标：建立纯计算、可演进的成本/用量观测能力层（token 用量估算模型 + 价格档位模型），提供任务创建前的事前区间估值消费点 `POST /v1/tasks/estimate`。

## 2. 方案决策

| 方案 | 结论 |
| --- | --- |
| 能力层形态：纯计算模块（不落库、不出指标） | ✅ **采用**——预估值是噪声源，污染观测数据；落库/指标留给未来真实消费需求 |
| Mock AI / staging fake | ❌ 已否决（用户决策：全量真实 Key） |
| 前端计算价格 | ❌ 已否决（价格常量单一事实源在后端，前端会漂移） |
| 预估金额落任务记录 | ❌ 已否决（动 schema 三处，事后有真实 cost_estimate，对账价值不足） |
| 预估调用出 Prometheus 指标 | ❌ 已否决（YAGNI，未来消费需求出现再加） |

## 3. 核心能力层（services/generation/）

### 3.1 token 用量估算模型（新增 `token_estimator.py`）

估算常量集中定义，注释登记来源与校准条件；换模型/换书籍时单点校准：

| 常量 | 值 | 来源 |
| --- | --- | --- |
| `PROMPT_TOKENS_PER_KP` | 1500 | R1 live 实测 1,427/单元（85,599/60），向上取整偏保守 |
| `OUTPUT_TOKENS_PER_KP` | 3300 | R1 live 实测 3,263/单元（195,774/60），向上取整偏保守 |
| `CUSTOM_REQ_TOKENS_PER_CHAR` | 0.5 | 约定：custom_requirements 每字符约 0.5 token |

纯函数映射（可单测、确定性）：

- 知识点数 = 章节数 × 密度系数（`COMPACT=1 / BALANCED=2 / EXTENSIVE=3`，与 V4 规划同口径）
- prompt_tokens = 知识点数 × PROMPT_TOKENS_PER_KP + 字符增量（custom_requirements 长度 × CUSTOM_REQ_TOKENS_PER_CHAR）
- output_tokens = 知识点数 × OUTPUT_TOKENS_PER_KP

### 3.2 区间估算

- `price_low` = 全部 prompt 命中缓存 + output 固定价 → `estimate_cost_by_kind(prompt_tokens, 0, output_tokens)`
- `price_high` = 全部未命中 + output 固定价 → `estimate_cost_by_kind(0, prompt_tokens, output_tokens)`
- 复用 `cost.py` 公开入口（生效日期取档），不触碰私有 `_price_for`、不重复定义价格

### 3.3 价格档位模型（现有 `cost.py`，保持不动）

- 价格常量、按生效日期取档、事后核算（`estimate_cost` / `estimate_cost_by_kind`）不修改
- 演进性：价格调整只改 cost.py 档位；模型/书籍变化只校准 token_estimator 常量；新消费点（预算告警、任务详情预估、动态 hit 率校准）复用两模块

## 4. 消费点：`POST /v1/tasks/estimate`

| 项 | 值 |
| --- | --- |
| 方法/路径 | `POST /v1/tasks/estimate` |
| 请求体 | `{ "selected_chapters": [uuid...], "generation_config": GenerationConfig }`，与 `POST /tasks` 同构，校验复用（空数组/非法 config → 422） |
| 响应 | `{ "knowledge_point_count": int, "estimated_card_count": int, "price_low": float, "price_high": float, "currency": "CNY" }` |
| 语义 | 无副作用、不落库、豁免幂等键（`/samples` 先例）、不需要 API Key（纯计算） |
| 前端展示 | 「预计 ¥0.04 ~ ¥0.08」（price_low/price_high 单位元） |

## 5. 契约同步（红线 1 三处一致）

- `structure-contract.md`：8.4 成本估算附近新增「token 用量估算模型」能力口径（常量来源/密度映射/区间定义）；6.x 新增 `POST /tasks/estimate` 接口（请求/响应/错误码）
- `openapi.yaml`：新增路径 + `CostEstimateResponse` 组件
- `app/schemas/tasks.py`：预估请求/响应模型（守卫锚点）
- 错误码：无新增（422 VALIDATION_ERROR 复用现有口径）

## 6. 测试

- 估算模型单元：密度系数×章节数确定性、区间单调 low≤high、token 常量、custom 增量
- 接口集成：空章节/非法 config 422、无 Key 可用、无副作用（预估后无表写入）、设备隔离语义
- 守卫：预估 schema ↔ openapi 一致
- 全量回归：现有 366+ 测试不得破坏

## 7. 文档联动

- `handoff-2026-08-12.md`：Mock AI 方向关闭（全量真实 Key）、前端待办 #5 改写、新增预估接口对接说明
- `Progress.md`：登记新工作包与审计日期
