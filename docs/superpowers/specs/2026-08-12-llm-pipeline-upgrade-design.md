# LLM 链路升级设计（LLM Pipeline Upgrade）

日期：2026-08-12｜状态：DRAFT（待用户审阅后冻结）

## 1. 背景与目标

现状缺陷（均已核实）：

- generator v1/v2 仅有格式包装差异，prompt 内容未实质升级；`prompts/v2` 与 `rubrics/scoring-prompt.md` 是死资产（运行时从未调用）。
- 生产链路生成调用输入只有知识点 topic，**原文分片内容从未进入调用**（`batches.py` prompt 组装仅 `topic_list`；scanner 只落章节元数据，文本样例不落库）——"内容严格依据原文分片"的 PRD 语义在生产链路断裂，属 mock 掩盖缺陷（与 R-20 同性质）。
- `target_difficulty` 是批内 priority 轮换的假分配（`_record_rubric`），difficulty_ratio 未落实。
- 规划为确定性占位（`planning.py`），无真实规划语义；任务创建同步完成，无异步阶段。
- 批按 batch_size=3 分组、靠 offset 反推知识点；分布观测在批级，语义与"批=单元"不兼容。

总原则（用户收敛句）：**代码决定数量、配比、上限、ID 和状态机；Planner 只负责根据可靠来源，产出带"学习目标 + 目标难度 + 锚定卡型 + 来源引用"的生成单元。**

## 2. 范围

**做**：

1. 文本分片持久化（`text_chunks` 表）——planner/generator 的原文输入底座。
2. LLM 资产升级与激活：planner（规划）/ generator（生成）/ rewrite（重写）内容升级；scoring（评分）正式激活。
3. 任务创建异步规划：`PENDING + stage=PLANNING` → 抢占 → 规划 → `GENERATING`。
4. 批粒度 = 生成单元（1 单元 = 1 批 = 1 次生成调用），Batch 显式持有 `generation_unit_id`。
5. Scoring 分层抽样 + 合批/逐卡规则 + 独立 SCORING 阶段。
6. `llm_call_attempts` 调用账本（所有 LLM 调用记账，重试/上限权威）。
7. 估算接口删除 + 全局硬上限。
8. 契约同步（3.10 difficulty 1~10、6.10 分组键、PRD 5.4.1/5.6/5.7、database-design、openapi、红线 5 版本）。

**不做**（登记，另行工作包）：

- 加密改造（每用户密钥/信封加密——后续工作包，现状环境变量单密钥保持）。
- DeepSeek 适配层（模型/thinking 冻结不变）。
- `card.schema.json` v1（单卡校验保持）。
- 每单元 2/4 卡（"覆盖范围 + 练习深度"是 PRD 变更候选，需同步 PRD/前端文案/数量校验，另行工作包）。

## 3. 概念模型

### 3.1 生成单元（GenerationUnit）

"知识点"概念改名**生成单元**：最小规划单元 = 一个锚定卡片类型与目标难度的生成任务。数据库表名保持 `knowledge_points`（兼容壳），ORM 注释与契约/PRD 称谓更新。

字段（表加列见 §11）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `knowledge_point_id` | PK | 服务端生成 |
| `task_id` / `chapter_id` | FK | 保持 |
| `source_chunk_ids` | TEXT JSON（新列） | 服务端分片标识列表（`text_chunks.chunk_id`），LLM 只能引用不能编造 |
| `learning_objective` | 语义复用 `topic` 列 | 学习目标（planner 输出），不再用"第X章-知识点N"占位 |
| `target_difficulty` | String（新列） | BASIC / UNDERSTANDING / APPLICATION（规划锚定） |
| `card_type` | String（新列） | QUESTION / TRUE_FALSE（规划锚定） |
| `priority` | int | 全局顺序（服务端合并后分配） |
| `status` | String | PENDING / PROCESSED / SKIPPED（保持） |

### 3.2 双维锚定（卡型 × 难度）

卡型（QUESTION/TRUE_FALSE）与目标认知难度（BASIC/UNDERSTANDING/APPLICATION）是两个独立维度，规划时同时锚定，生成时遵循，互不派生。

- `Card.target_difficulty` 由规划锚定落库；**删除 `_record_rubric` 的 priority 轮换假分配**（`batches.py` 中 carry-forward 逻辑）。
- generator prompt 输入含 `target_difficulty` + `card_type` 锚定；输出卡型/难度与锚定不一致 → 代码校验拒绝（批次重试预算）。

### 3.3 组合规则（决策拍板）

| 难度 | 卡型 | 形态 | 约束 |
| --- | --- | --- | --- |
| BASIC | QUESTION（默认）/ TRUE_FALSE | 原子事实/定义直问或二值判断 | 判断题表述无歧义 |
| UNDERSTANDING | QUESTION / TRUE_FALSE | 对比/推理/因果 | 同上 |
| APPLICATION | QUESTION（默认） | 开放性问题（场景化提问，答案多角度分析） | 默认形态 |
| APPLICATION | TRUE_FALSE（允许） | **场景判断题** | ① 需应用规则/概念才能判断（不能是事实换皮）；② 结论可明确二值化；③ `explanation` 给出判断依据 |

校验层不做语义拦截（无法可靠判断"是否事实换皮"）——由 Prompt 约束 + Rubric 观测（正确答案/证据维度分数反映）。

### 3.4 密度与单元预算（决策拍板：密度只控制预算）

- **每单元 1 卡**（N=1 固定）：generator 输出恰好 1 张锚定类型卡；回归 v2 live 验证语义（每知识点一张卡）；乘法放大消除；批部分成功歧义同步消除。
- 单元预算公式保持 V4 可测口径：`每章基础分片 3 × 密度系数`（COMPACT=1 / BALANCED=2 / EXTENSIVE=3）→ 2 章 6/12/18。预算语义 = **上限**（PRD 5.4.1"不代表固定卡片数量"）：planner 输出可少于预算（内容不足），超限按 priority 截断。
- 数量关系 COMPACT < BALANCED < EXTENSIVE 由预算上限保证（LLM 无需自我计数）。
- 预算与真实分片数解耦（真实分片只影响规划输入与引用，不影响预算公式）。

### 3.5 难度配额算法（决策拍板：代码计算并校验）

**三层确定性分配，全部代码计算，最大余数法（largest remainder），固定顺序消除随机性**（配额并列余数时按 BASIC < UNDERSTANDING < APPLICATION、章序、组序）：

1. **任务总配额**：任务总预算（章节数 × 3 × 密度系数）× difficulty_ratio，最大余数法取整到总和 = 总预算。例：预算 6、40/40/20 → 3/2/1（6×0.4=2.4→2、2.4→2、1.2→1，余数 0.4/0.4/0.2，缺额 1 补给余数最大者，并列取 BASIC）。
2. **章配额**：每个难度的总配额按章确定性分发（按各章预算占比，最大余数法）。
3. **子配额**：超长章节拆多次调用时，按各规划分组的 `char_count` 占比分配子配额（最大余数法）。

约束：每次调用只拿自己的子配额（不得把整章配额重复发给每个调用）；每调用输出 ≤ 子配额（超限按 priority 截断）；实际输出允许少于配额；实际难度分布记录观测（不强制补满）。

## 4. 文本分片持久化（`text_chunks`）

- scanner PARSED 时：解析文本层 → 按页区间稳定分片（Settings `text_chunk_pages`，默认 8 页/片）→ 新表 `text_chunks`（chunk_id PK、file_id、chapter_id、page_start、page_end、char_count、content TEXT）落库。
- 重解析幂等：处理前清理该 file_id 的既有分片（同 chapters 定式）。
- `source_chunk_id` 语义：服务端分片标识（chunk_id），planner 输出必须 ⊆ 服务端分片集合（代码校验）。
- AC-08"文本样例不落库"指日志/请求审计样例；功能数据存分片不受影响（structure-contract 注明）。
- 无文本层章节：无分片 → 规划时该章跳过（0 单元），计数进入空单元/部分成功语义（§6.4）。

## 5. LLM 资产与输出契约

### 5.1 版本布局（保留 v1/v2，审计链完整）

| 资产 | 现状 | 新版本 |
| --- | --- | --- |
| `prompts/` planner | v1（死资产） | `prompts/v3/planner.md`（激活） |
| generator | v1/v2（v2 仅包装修复） | `prompts/v3/generator.md`（继承 v2 包装指令） |
| rewrite | v1 | `prompts/v3/rewrite.md`（内容升级） |
| `rubrics/` main + scoring-prompt | v1（scoring 死资产） | `rubrics/v2/rubric.md` + `scoring-prompt.md`（正式激活） |
| `schemas/` card | v1（保持） | 新增 `schemas/v2/planner-output.schema.json`、`scoring-output.schema.json` |

- manifest：generator v2→v3、planner v1→v3、rewrite v1→v3、rubrics.main v1→v2、schemas 新增两入口；CHANGELOG 追加。
- 红线 5：structure-contract 3.5 版本字段同步（schema_version 扩展为多入口表达：card v1 + planner-output v2 + scoring-output v2）。

### 5.2 Planner 输出契约

顶层对象（适配 `response_format=json_object`）：

```json
{
  "units": [
    {
      "source_chunk_ids": ["chunk-01", "chunk-02"],
      "learning_objective": "比较 ReAct 与 Plan-and-Execute 的决策差异",
      "target_difficulty": "UNDERSTANDING",
      "card_type": "QUESTION",
      "priority": 1
    }
  ]
}
```

- 输入：分片文本（服务端提供）+ 该组子配额（各难度允许数量）。
- `schemas/v2/planner-output.schema.json` 校验结构；代码层（`services/generation/planner_validator.py`，仿 schema_validator 定式）校验：结构合法、单元数 ≤ 子配额、`source_chunk_ids` ⊆ 服务端分片、锚定值枚举合法。
- prompt 描述（决策拍板口径）：三档密度的粒度定义与相对数量关系、按卡型规划形态（BASIC 原子知识点 / UNDERSTANDING 理解主题 / APPLICATION 开放性问题或场景判断题）、组合规则 §3.3 的约束（判断题不可事实换皮、须可二值、explanation 给依据）。

### 5.3 Generator 输出契约

- 每单元一次调用；输出 `{"cards": [恰好 1 张卡]}`（包装指令继承 v2）；输入 = 学习目标 + 锚定难度/卡型 + 分片文本 + schema。
- 代码校验：卡型 = 锚定、数量 = 1；非法 → 批次重试预算（现有语义）。
- `card.schema.json` v1 保持（单卡校验）。

### 5.4 Scoring 输出契约

```json
{"scores": [{"generation_item_id": "...", "evidence_score": 2, "correctness_score": 3, "difficulty_score": 2, "learning_value_score": 2, "rubric_total_score": 9}]}
```

- `schemas/v2/scoring-output.schema.json` 校验；分数范围 0~3/维、总分 0~12。
- 基础类（BASIC/UNDERSTANDING）合批时多卡一次调用（数组）；应用类逐卡。

### 5.5 输出校验层

`planner_validator.py` / `scoring_validator.py`（仿 `schema_validator.py` 定式）：加载 manifest schema 资产 + 代码规则（数量/锚定/来源/分数范围）。Generator 复用 `validate_card`（单卡）+ 锚定校验（卡型/难度/数量）。

## 6. 任务状态机（PLANNING 阶段）

### 6.1 状态与抢占

```
POST /tasks → status=PENDING, stage=PLANNING（创建即返回，幂等保持）
```

**规划 worker**（新入口，条件更新 CAS，并发单执行者）：

- CAS1：`UPDATE ... WHERE status='PENDING' AND stage='PLANNING' → RUNNING`（rowcount=1 才接管）。
- CAS2（孤儿恢复）：`WHERE status='RUNNING' AND stage='PLANNING' AND updated_at < now - orphan_timeout_minutes → RUNNING`（心跳超时接管）。
- **普通 worker 不得直接扫描并执行所有 RUNNING 规划任务**（只走 CAS）。

**生成 worker**（现有 executor）：扫描 `RUNNING AND stage=GENERATING`（加 stage 条件，避免与规划中任务冲突）；批次级抢占沿用 V5B（PENDING/FAILED → PROCESSING 条件更新）；RUNNING+GENERATING 孤儿由扫描即处理 + resume 接口（现有语义）。

### 6.2 规划执行

1. 规划分组：按章 → 章分片文本总 char 数 ≤ Settings `planner_max_input_chars` → 一组一次调用；超限 → 按分片累计 char 拆组。
2. 组数上限：Settings `max_planner_calls_per_task`（全局硬上限，§10）；超限 → 任务 FAILED（§6.3）。
3. 每组调用（事务外，LLM 调用不入 SQLite 写事务）：输入分片文本 + 子配额 → 校验（schema + 配额 + 来源 + 锚定）→ **结果暂存进程内**（不落库）；每调用记 `llm_call_attempts`（operation_key = `planning:{chapter_id}:{group_index}`）；每组后刷新心跳并 commit（批次事务粒度，孤儿判据不误判）。
4. 全部组完成后：**跨调用去重**——指纹 = 规范化后的 `(learning_objective, target_difficulty, card_type, source_chunk_ids)`（分片列表排序；同一分片可合法产生多个学习目标，不得按 source_chunk_ids 单独去重）；按调用顺序分配全局 priority。
5. 同事务落库：units（含新列）+ `plan_batches`（1 单元 1 批 + generation_unit_id）+ 任务 `stage=GENERATING`（status=RUNNING）+ 实际难度分布记录。
6. 落库前会话内复查任务状态（被 cancel → 放弃写入）。
7. 崩溃恢复：任务回到 PENDING/孤儿 → 重新规划；重试预算以账本 `operation_key` FAILED 计数为准（不重置）。

### 6.3 错误分类与重试（账本为权威）

| 类别 | 判定 | 处理 |
| --- | --- | --- |
| Key 错误 | 解密失败 / 上游 401 → API_KEY_UNAVAILABLE | 任务 FAILED + failure_stage=PLANNING，不重试 |
| 上游暂时失败 | 429 / 5xx / 网络 | 该组重试（预算 2 次重试 = 3 次尝试，账本 `operation_key` FAILED 计数）→ 超限组 SKIPPED |
| 输出非法 | JSON/schema/配额/来源/锚定 | 同上游（预算内重试）→ 超限组 SKIPPED |
| 硬上限 | 组数 > `max_planner_calls_per_task` | 任务 FAILED + failure_stage=PLANNING + error_code=GENERATION_FAILED（复用兜底码，日志注明） |

### 6.4 空单元语义（决策拍板：有条件 COMPLETED(0)）

- **全组成功但合法单元 0 个**（含无文本层章节）：COMPLETED，`total_batch_count=0`、`generated_card_count=0`、`completion_reason=NO_GENERATION_UNITS`（tasks 新列）——业务合法结果（PRD 5.6"无有效学习内容返回空数组"）。
- **全部规划组因上游/输出错误失败**：FAILED + failure_stage=PLANNING（不伪装成业务空结果）。
- **部分组失败、部分成功**：成功组继续生成；`skipped_planning_group_count`（tasks 新列）记录跳过组数。

### 6.5 cancel / resume / 删除保护

- cancel/resume 覆盖 PENDING/RUNNING（含 PLANNING 阶段），现有条件更新语义扩展。
- V1 删除保护（非终态 PENDING/RUNNING/PAUSED 引用 → 409 TASK_IN_PROGRESS）已含 PENDING，无需改动。

## 7. 批关联与观测

- Batch 加列 `generation_unit_id`（FK knowledge_points）；`plan_batches` 按单元建批（每单元一批、batch_index=1..N、显式外键）；**删除 offset 反推逻辑**（batches.py 按 `(batch_index-1)*batch_size` 取 kp 的代码）。
- 每单元 1 卡 → 部分成功歧义消除：SUCCEEDED = 恰好 1 张合法卡；0 张 → 重试/SKIPPED（现有语义不变）。
- 观测字段语义文档化：`coverage_rate` = 该单元是否产出合法卡（0/1），不再恒定 1.0；distribution 字段为单值（quality-summary 聚合逻辑不变）。
- `Card.target_difficulty` 由规划锚定落库（`_record_rubric` 轮换删除；`target_difficulty` 不再在评分时补写）。

## 8. Scoring（独立 SCORING 阶段 + 分层抽样）

- **独立阶段**：GENERATING 批循环全部完成 → 任务 `stage=SCORING`（status=RUNNING，心跳/孤儿沿用）→ 评分完成 → COMPLETED。理由：合批跨批（多单元一次调用），批内立即评分无法合批；抽样需要完整单元列表（规划后确定）。cancel 覆盖 SCORING。
- **预先分层抽样**：规划完成后按（章节 × 难度 × 卡型）分层抽样预选评分单元，调用上限 Settings `max_scoring_calls_per_task`（默认 60，约束调用数）。
- **组批规则**：BASIC/UNDERSTANDING 单元按章合批（多单元一次评分调用）；APPLICATION 每单元单独调用（每单元 1 卡 = 每卡一次）。组批后调用数 > 上限 → 确定性缩减（按 priority 保留）。
- 评分调用记 `llm_call_attempts`（stage=scoring）；失败不重试不阻塞（观测缺省，卡照常入库——PRD 5.9"不参与入库"不变），账本记 FAILED。
- 评分结果校验（scoring-output schema + 分数范围）后落 Card 5 字段 + 批次质量字段（`_record_rubric` 改造为 LLM 输出解析 + 锚定难度，删除轮换）。
- Rubric 观测组合规则（§3.3）：场景判断题的"事实换皮/依据缺失"由 correctness/evidence 分数反映，不做语义拦截。

## 9. `llm_call_attempts` 调用账本（新表）

所有 LLM 调用记账，**重试预算与调用上限的权威**（Task 上不设冗余计数列；planner_attempts 单列不足以表达多章多分组，废弃该思路）：

| 列 | 说明 |
| --- | --- |
| call_id | PK |
| task_id | FK |
| stage | PLANNING / GENERATING / SCORING / REWRITE |
| operation_key | `planning:{chapter_id}:{group_index}` / `generating:{batch_id}` / `scoring:{group_key}` / `rewrite:{card_id}` |
| model / prompt_version | 实际值与版本 |
| cache_hit / cache_miss / output_tokens | usage 原样 |
| http_status / duration_ms | 观测 |
| status | SUCCESS / FAILED |
| error_code | 失败类别 |

- 重试判定：该 operation_key FAILED 计数 ≥ 预算 → 组 SKIPPED（崩溃恢复后仍严格成立）。
- Scoring 上限：账本 stage=scoring 调用数 ≤ 上限（抽样预保证 + 账本校验）。
- 8.3 指标（llm_requests_total 等）沿用 llm_metrics 上报，账本是持久化视图。

## 10. 估算接口删除与全局硬上限

**删除**（用户拍板）：`/tasks/estimate` 路由、CostEstimateRequest/Response schema、`token_estimator.py`、openapi 路径与组件、structure-contract 8.4 能力层 / 6.4 接口行、相关测试。**保留** `cost.py`（批 cost_estimate 观测 + quality-summary 成本汇总仍用）。`planning.py` 整体重构为单元预算计算（knowledge_point_count 删除）。

**新增全局硬上限**（Settings，删除估算前必须落地，防"每章上限随章节数无限增长"）：

- `max_generation_units_per_task`（默认 300）：规划单元总数超限 → 任务 FAILED（§6.3 硬上限）。
- `max_planner_calls_per_task`（默认 30）：规划调用组数超限 → 任务 FAILED。

## 11. 数据库迁移（0003）

| 表 | 变更 |
| --- | --- |
| `knowledge_points` | + `target_difficulty` String NULL、+ `card_type` String NULL、+ `source_chunk_ids` TEXT NULL（旧数据无值，新数据代码保证必填） |
| `batches` | + `generation_unit_id` String NULL（FK knowledge_points.knowledge_point_id） |
| `tasks` | + `completion_reason` String NULL、+ `skipped_planning_group_count` Integer NULL |
| `text_chunks`（新表） | chunk_id PK、file_id FK、chapter_id FK、page_start/page_end、char_count、content TEXT、created_at；索引 file_id、chapter_id |
| `llm_call_attempts`（新表） | 见 §9；索引 (task_id, stage, operation_key) |

空库 upgrade/downgrade/upgrade 往返实测；`alembic check` 零漂移。

## 12. 契约同步

| 位置 | 改动 |
| --- | --- |
| structure-contract 3.10 | ReviewState difficulty 描述 0~10 → **1~10**（对齐 database-design/ORM CHECK） |
| structure-contract 6.10 | 补 quality-summary 分组键定义：model = batch.model；pdf = task.file_id；**difficulty = 卡片锚定 target_difficulty**（质量聚合改按 Card 锚定分组，不再解析批难度分布最大档——实现简化） |
| structure-contract 3.5/3.6 | 生成单元概念（3.1）、双维锚定（3.2）、组合规则（3.3）、密度预算与配额（3.4/3.5） |
| structure-contract 3.7 | 批=单元观测语义（§7）、llm_call_attempts 账本（§9）、scoring 阶段 |
| structure-contract 3.5 版本字段 | prompt_version v3、rubric_version v2、schema_version 多入口（card v1 + planner-output v2 + scoring-output v2） |
| PRD 5.4.1 | 密度 = 单元预算（上限），删除"密度影响知识点数量"的可测口径表述 → 预算公式口径 |
| PRD 5.6 | 生成单元概念、card_type/target_difficulty 锚定、综合应用=开放性问题/场景判断题（非原子聚合） |
| PRD 5.7 | 题型由规划锚定（不再生成期自动选择）；"内容严格依据原文分片"由 text_chunks 输入落实 |
| database-design | 2.5 tasks / 2.6 knowledge_points / 2.7 batches 列变更 + 新表 text_chunks、llm_call_attempts |
| openapi | KnowledgePoint/Batch/Task schema 更新、`/tasks/estimate` 删除、CostEstimate 组件删除 |
| 红线 5 | manifest ↔ structure-contract 版本一致 |
| R-03 | 本工作包覆盖（**PLANNED**——迁移/实现/守卫/验收通过后才能改 RESOLVED） |

## 13. 测试与验收

四工具全绿 + 守卫全绿（schema↔openapi、ORM↔database-design、错误码↔契约、manifest↔运行时版本）。

新增测试：

- 迁移 0003 往返；text_chunks 持久化与重解析幂等。
- 配额算法：预算 6 / 40/40/20 → 3/2/1 确定性；章分发、子配额（char_count 占比）、并列余数固定顺序。
- 规划状态机：CAS1（PENDING→RUNNING）、CAS2（孤儿接管，非孤儿拒绝）、取消（落库前放弃写入）、账本重试预算（崩溃恢复不重置）、空单元三分支（NO_GENERATION_UNITS / FAILED+PLANNING / 部分成功+skipped 计数）、硬上限 FAILED。
- 分批规划：分组拆分（char 预算）、跨调用指纹去重（同分片多目标合法、重复指纹去重）、全局 priority 分配。
- 锚定校验：卡型=锚定、难度=锚定、数量=1、来源 ⊆ 服务端分片。
- Scoring：分层抽样、合批/逐卡、上限缩减、SCORING 阶段状态机、失败不阻塞。
- llm_call_attempts：记账全阶段、operation_key 重试判定、scoring 上限校验。
- 估算删除：路由/schema/token_estimator 无引用。
- V4/V5A/V6 测试更新：fake 规划 → mock planner 契约断言；`_record_rubric` 轮换删除 → 锚定落库。

live 验证（真实 Key、预算上限）→ 后续 R 级工作包（登记，不属本包完成条件）。

## 14. 登记与风险

- R-03：本工作包覆盖（PLANNED），验收后 RESOLVED。
- 加密改造（每用户密钥/信封加密）：后续工作包。
- 每单元 2/4 卡（练习深度）：PRD 变更候选，另行工作包。
- R-17（SQLite 单写者）不变：规划 LLM 调用在事务外，长调用不持写锁；评分阶段同。
- 规划结果暂存进程内（未落库中间结果）：崩溃重规划，账本防无限重试——接受（与批次重试同性质）。
- 场景判断题语义依赖 Prompt + Rubric 观测，不做代码语义拦截（登记为观测性约束）。
