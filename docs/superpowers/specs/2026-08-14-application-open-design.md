# 综合应用卡开放化设计（APPLICATION Open-Design）

日期：2026-08-14｜状态：DRAFT（设计经用户逐段确认，待 spec 审阅后进入实施）

## 1. 背景与目标

现状缺陷（均已核实）：

- generator v3 把 APPLICATION（综合应用）实现成"闭合判定"：把来源规则用于边界明确的最小
  场景，问题必须明确要判断/选择/解释什么，禁用宽泛"请多角度分析"。与契约原意漂移——
  `structure-contract.md` 3.6 组合规则与 194 行本写"开放性问题（场景化提问，答案多角度
  分析）"，prompt 实现收窄。
- 组合规则允许 `APPLICATION × TRUE_FALSE`（场景判断题），把"深度"绑上"二值判定"，与
  开放题语义冲突（用户判定：判断题只是独立于问答的提问方式，无标准答案的题不适合判断
  题）。
- rubric v2 的"答案正确性"维度假设答案可验证，对无标准答案的开放题失效。

用户目标（2026-08-14 逐项确认）：

1. 综合应用卡 = **开放深问**（设计 / 论证 / 评估 / 权衡 / 破题），题干前提可溯源到来源
   页，**无标准答案**，`answer` 承载多角度参考思路；
2. 取消场景判断题组合：APPLICATION 仅 `QUESTION`，TRUE_FALSE 保留给 BASIC / UNDERSTANDING；
3. **生成侧走思考模式**：APPLICATION 的生成与评分 LLM 调用开 thinking（沿用
   `deepseek-v4-flash` + thinking 开关，不引入新模型）；
4. 数据模型零变更：复用 `answer` 字段承载思路，不动 DB / openapi / 复习侧。

目标形态示例（用户提供，写入 generator 静态示例，保留"示例事实不得用于实际任务"约束）：

1. 护栏部分提到了工具风险评级。如果一个工具在大多数情况下是低风险的，但在特定参数组合
   下变为高风险（如 delete_file 删除普通文件 vs 删除系统文件），你会如何设计动态风险评估？
2. "好的设计原则应该穿越模型的迭代周期"。试举一个你认为可能会随模型进步而过时的当前
   Agent 设计原则，并说明理由。
3. ReAct 循环中，Agent 的每一次 LLM 调用都会看到完整的历史轨迹。随着轨迹增长，这种设计
   的成本是二次方增长的。有没有办法在不丢失关键信息的前提下打破这个二次方？

## 2. 范围

**做**：

1. 资产新版本：`prompts/v4/`（planner / generator / rewrite）+ `rubrics/v3/`（rubric /
   scoring-prompt），更新 `manifest.json`，追加 `agent_evolution/CHANGELOG.md`，与契约同步
   同次提交（`agent_evolution/AGENTS.md` 原子性规则）。
2. llm 层 per-call thinking 覆盖参数 + APPLICATION 路由（生成与评分两处调用点）。
3. `structure-contract.md` 同步：3.6 组合规则、难度枚举描述、`prompt_version` /
   `rubric_version`。
4. PRD V2.1 同步：5.4.2 难度定义表、5.6 单元产出描述、231 行旧措辞、验收标准。默认
   **原地修订 `prd_v2_1.md`**（对既有定义的修正，不升版本号；用户可在 spec 审阅时改主意）。
5. 测试与真实 canary 验收（见 §6）。

**不做**（登记，另行工作包）：

- `card.schema.json` v1 / `generator-output.schema.json` v2 / `scoring-output.schema.json` v2
  变更（开放题仍是 `QUESTION {question, answer}`）；
- DB 迁移、`openapi.yaml`、`database-design.md` 变更；
- 复习侧改动（前端思考模式、Again/Hard/Good/Easy 评级适配）；
- 难度默认配额变更（40/40/20 不变）；
- 新增模型 ID / 更换模型（沿用 `deepseek-v4-flash` + thinking 开关）。

## 3. 资产语义设计

### 3.1 planner v4 —— APPLICATION 单元判定

| 目标难度 | 必须检索的认知动作 | 不合格形态 |
| --- | --- | --- |
| APPLICATION | 围绕来源中的原则、机制、设计权衡或技术决策，提出一个有真实思考空间的开放问题（要求设计、论证、评估、权衡或破题），且题干前提可溯源到来源页 | 把原子事实套上"你怎么看"式开放问句（回忆换皮）；前提不可溯源；问来源之外的主题 |

- 与 UNDERSTANDING 的边界：UNDERSTANDING = 答案在来源内可判定（原因/机制/关系/差异/
  条件/后果）；APPLICATION = 答案无法在来源内直接判定、需学习者自己综合/设计/论证，但
  题干所依据的前提在来源内。
- 卡型限制：APPLICATION 仅 `QUESTION`；单元判定表删除 `APPLICATION + TRUE_FALSE` 相关
  约束行。
- `learning_objective` 认知动作词改为：设计、论证、评估、权衡、提出方案、分析（不再用
  "判断、应用"）。
- `source_chunk_ids` 最小充分来源 = 题干前提所在的页（思路不需要来源支撑，见 3.2）。

### 3.2 generator v4 —— APPLICATION 定义与信任边界分级

**难度节新定义**（替换 v3 现有 APPLICATION 行）：

> `APPLICATION`：围绕来源中的原则、机制、设计权衡或技术决策提出开放深问。题干前提（所
> 依据的事实、原则、机制）必须来自来源并忠实呈现；问题要求设计、论证、评估、权衡或提
> 出方案，无标准答案；`answer` 写参考思路（2～4 个角度，最有价值的角度在前），思路必须
> 自洽、具体、不误导。

**信任边界分级**（对 v3 信任边界节做 APPLICATION 例外条款，这是本次最大语义放开）：

- 题干前提仍唯一来自 `source_material`，照旧溯源，不得编造或歪曲；
- `answer`（参考思路）允许模型自身知识——这是"应用"的本义（如"打破二次方"的思路必然
  涉及来源外的技术）；
- 三条硬约束：① 思路必须与来源前提衔接，不得换主题；② 不得把模型知识伪装成来源结论
  （不写"根据原文可得……"）；③ 来源不足以支撑一个有思考空间的开放问时，输出
  `{"cards":[]}` 弃权。

**长度软目标调整**（准确性、前提溯源和可思考性优先，不得为凑字数删关键信息）：

- APPLICATION `question`：通常不超过 60 汉字 / 30 词（深问题干常需带前提，如示例 1）；
  BASIC / UNDERSTANDING 的 45 汉字 / 24 词软目标不变；
- APPLICATION `answer`：150～400 汉字 / 80～220 词，最多 4 个要点（从 80～220 汉字 /
  40～130 词放宽）。

**静态示例**：删除"示例 2：APPLICATION + TRUE_FALSE"，替换为 §1 的三则开放深问示例
（三则全收：设计 / 论证 / 破题各一，覆盖提问方式谱系），沿用"示例只校准提问方式、粒度、
文风和 JSON 形状；示例事实不得用于实际任务"约束——三例恰好是 Agent 教材真实知识，实际
来源涉及同主题时模型必须以来源为准。

**卡型语义节**：删除 `APPLICATION + TRUE_FALSE 必须先把规则用于场景条件才能判断` 一行；
`TRUE_FALSE` 语义仅保留给 BASIC / UNDERSTANDING。

### 3.3 rewrite v4 —— APPLICATION 卡重写约束

- v3 约束"不新增未经提供材料支持的事实"对 APPLICATION 卡分级：题干前提仍受来源约束；
  参考思路允许调整角度、精炼或重排，但不得改变主要论证方向、不得新增误导性事实、不得
  把开放题改成闭合判定题。
- "保持原问题考查的同一知识主题和答案结论"对 APPLICATION 改为"保持问题的开放性与思考
  空间、思路的主要角度与结论倾向"。
- 输出契约不变：QUESTION 仍只输出 `type`、`question`、`answer`。

### 3.4 rubric v3 + scoring-prompt v3 —— 四维口径按难度分化

四维名称与 DB 列不动（避免迁移），只改对 `target_difficulty == APPLICATION` 卡片的评估
口径：

| 维度 | APPLICATION 口径 |
| --- | --- |
| 原文依据 | 题干前提由当前来源支持；思路与前提相扣；未把模型知识伪装成来源结论 |
| 答案正确性 → 思路质量 | 思路多角度、逻辑自洽、具体可行、无事实性误导；空洞复述题干 = 低分 |
| 难度匹配 | 学习者必须真实完成设计/论证/权衡才能给出有质量的回答；"套开放问句的回忆题" = 1 分 |
| 学习价值 | 开放问确有思考空间（不是"你怎么看"式空问）；思路深度与复习负担匹配 |

- 3 分口径表中 APPLICATION 行替换为开放问口径；删除 APPLICATION × TRUE_FALSE 相关行。
- 紧凑校准例 1/2（涉及场景判断题）替换为开放题口径示例（如：回忆换皮只降难度匹配与学习
  价值；思路把模型知识伪装成来源结论时降原文依据）。
- 原文依据维度中"APPLICATION 场景不必逐字出现在来源中"段落改写为"题干前提必须溯源；
  参考思路不要求来源支撑，但不允许歪曲来源或伪装溯源"。
- 评分 LLM 调用对 APPLICATION 卡开 thinking（见 §4）。

## 4. LLM 路由设计

- `DeepSeekClient.chat` 增加 per-call 覆盖参数 `thinking: bool | None = None`：
  - 显式值优先；`None` 回落 `settings.deepseek_thinking`（`deepseek.py:122-126` 现有显式
    携带逻辑只加优先级，不重构适配层）。
- 路由规则：

| 调用点 | 条件 | thinking |
| --- | --- | --- |
| `services/generation/batches.py` 生成调用 | `unit.target_difficulty == APPLICATION` | enabled |
| 同上 | 其余难度 | 回落 settings（默认 disabled） |
| `services/generation/scoring.py` 评分调用 | `card.target_difficulty == APPLICATION` | enabled |
| 同上 | 其余难度 | 回落 settings |
| Planner 调用 | 全部 | 回落 settings（不产出卡片，控成本） |

- 模型 ID 不变；成本已确认：`usage.completion_tokens` 已含 reasoning token 计费
  （`deepseek.py:181-183` 映射现成），cost 层零改动。
- 观测：`llm_metrics.observe_llm_call` 增加 `thinking` 标签（成本归因，canary 核对用），
  不新增 DB 列。

## 5. 契约与 PRD 同步清单

同次提交：

- `agent_evolution/prompts/v4/{planner,generator,rewrite}.md` 新建；
- `agent_evolution/rubrics/v3/{rubric,scoring-prompt}.md` 新建；
- `agent_evolution/manifest.json`：`prompts.planner/generator/rewrite` → v4，
  `rubrics.main` 与 `prompts.scoring` → v3；schemas 节不动；
- `agent_evolution/CHANGELOG.md` 追加；
- `docs/Architecture/structure-contract.md`：3.6 组合规则表删 `APPLICATION × TRUE_FALSE`
  行、`APPLICATION × QUESTION` 形态改"开放深问（设计/论证/破题；无标准答案；
  answer=参考思路）"；难度枚举（166 行附近）删"或场景判断题"；`prompt_version` /
  `rubric_version` 同步（红线 5）；
- `docs/PRD/V2.1/prd_v2_1.md`：5.4.2 难度定义表综合应用行（改"组合多个知识点"为开放深问
  语义）、5.6 单元产出描述（296 行"场景化提问、答案多角度分析"升级为开放深问）、231 行旧
  措辞清理、相关验收标准；加修订注记。
- `docs/Progress.md`：进度注记。
- `openapi.yaml`、`database-design.md`：零变更（资源模型与表结构未动，红线 1/2 自动满足）。

## 6. 测试与验收

单元测试（`main/tests/`，命名 `test_<模块>_<行为>`）：

- llm 层：chat 的 thinking 覆盖参数组装（fake transport 断言 body——显式值 / None 回落
  settings）；
- 路由：APPLICATION 单元生成调用 → body 携带 `{"type": "enabled"}`；非 APPLICATION →
  回落；APPLICATION 卡评分调用 → enabled；
- 资产加载：manifest 新版本路径可加载。

回归：

- generator-output schema 校验不变（开放题仍是 QUESTION 分支）；
- 评分管线四维落库、总分层级、配额分配（40/40/20）不变；
- planner 输出约束：APPLICATION 单元不再锚定 TRUE_FALSE（prompt 约束；校验层本就不做
  语义拦截，由 prompt + rubric 观测兜底）。

样卡管线核对：

- `services/generation/samples.py`、`scripts/gen_sample_cards.py`、`fake.py` 中综合应用
  样本内容与形态对齐新语义（样卡 PRD 5.5 允许示例性情境，天然兼容开放题）。

真实 canary（必须真实跑，不能只看单测）：

- 用 `res/` 样书跑一次正式生成，人工检查 APPLICATION 卡的提问方式与深度是否达到 §1 三例
  水平；检查成本增量（llm_metrics）与弃权率。

## 7. 风险

- 深题质量依赖推理模型能力，深度可能不稳 → rubric v3 的难度匹配/学习价值维度观测 +
  canary 人工验收兜底；
- thinking 成本上升 → APPLICATION 默认配额 20% 限流 + metrics `thinking` 标签归因；
- 静态示例事实复用风险（三例是真实 Agent 知识）→ 保留"示例事实不得用于实际任务"约束，
  canary 时抽查是否有示例事实泄漏。
