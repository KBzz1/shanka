# LLM 链路升级设计（LLM Pipeline Upgrade）

日期：2026-08-12｜状态：FROZEN（用户审阅并优化后定稿，2026-08-12）

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

1. 按文件页码持久化文本（`text_chunks` 表，一页一行、与章节解耦）——planner/generator
   的原文输入底座；首个规划 worker 抢占任务时读取章节最新页码并冻结，随后按该规划快照
   选页、动态分组。
2. LLM 资产升级与激活：planner（规划）/ generator（生成）/ rewrite（重写）内容升级；scoring（评分）正式激活。
3. 任务创建异步规划：`PENDING + stage=PLANNING` → 抢占 → 规划 → `GENERATING`。
4. 批粒度 = 生成单元（1 单元 = 1 批 = 1 次生成调用），Batch 显式持有 `generation_unit_id`。
5. Scoring 分层抽样 + 合批/逐卡规则 + 独立 SCORING 阶段；抽样率与评分分母同步修正。
6. `llm_call_attempts` 调用账本（调用前持久化占位、所有尝试记账、规划合法结果可恢复，作为重试/上限权威）。
7. 估算接口删除 + 全局硬上限。
8. 契约同步（3.10 difficulty 1~10、6.10 分组键、PRD 5.4.1/5.6/5.7、database-design、openapi、红线 5 版本）。

**不做**（登记，另行工作包）：

- 加密改造（每用户密钥/信封加密——后续工作包，现状环境变量单密钥保持）。
- DeepSeek 模型、thinking 与 `response_format=json_object` 请求形态冻结不变；允许适配层最小扩展上游状态/`retryable` 元数据，以区分 401 与 429/5xx，除此之外不重构适配层。
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
| `source_chunk_ids` | TEXT JSON（新列） | 服务端页文本标识列表（`text_chunks.chunk_id`，一页一个），LLM 只能引用本次调用实际提供的页，不能编造 |
| `learning_objective` | 语义复用 `topic` 列 | 学习目标（planner 输出），不再用"第X章-知识点N"占位 |
| `target_difficulty` | String（新列） | BASIC / UNDERSTANDING / APPLICATION（规划锚定） |
| `card_type` | String（新列） | QUESTION / TRUE_FALSE（规划锚定） |
| `priority` | int | 全局顺序（服务端合并后分配） |
| `status` | String | PENDING / PROCESSED / SKIPPED（保持） |

兼容规则：旧列 `source_chunk_id` 本迁移不删除，新生成单元写入
`source_chunk_id = source_chunk_ids[0]` 作为兼容投影；运行时取原文一律以
`source_chunk_ids` 为权威。旧数据继续按原字段读取，后续破坏性迁移再删除兼容列。

### 3.2 双维锚定（卡型 × 难度）

卡型（QUESTION/TRUE_FALSE）与目标认知难度（BASIC/UNDERSTANDING/APPLICATION）是两个独立维度，规划时同时锚定，生成时遵循，互不派生。

- `Card.target_difficulty` 由规划锚定落库；**删除 `_record_rubric` 的 priority 轮换假分配**（`batches.py` 中 carry-forward 逻辑）。
- generator prompt 输入含 `target_difficulty` + `card_type` 锚定；输出卡型或数量不符合锚定
  → 代码校验拒绝（进入批次重试预算）。`target_difficulty` 不要求在 card schema v1
  中重复输出，由服务端写入规划锚定值；内容是否达到目标难度由 Rubric 观测。

### 3.3 组合规则（决策拍板）

| 难度 | 卡型 | 形态 | 约束 |
| --- | --- | --- | --- |
| BASIC | QUESTION（默认）/ TRUE_FALSE | 原子事实/定义直问或二值判断 | 判断题表述无歧义 |
| UNDERSTANDING | QUESTION / TRUE_FALSE | 对比/推理/因果 | 同上 |
| APPLICATION | QUESTION（默认） | 开放性问题（场景化提问，答案多角度分析） | 默认形态 |
| APPLICATION | TRUE_FALSE（允许） | **场景判断题** | ① 需应用规则/概念才能判断（不能是事实换皮）；② 结论可明确二值化；③ `explanation` 给出判断依据 |

校验层不做语义拦截（无法可靠判断"是否事实换皮"）——由 Prompt 约束 + Rubric 观测（正确答案/证据维度分数反映）。

### 3.4 密度与单元预算（决策拍板：密度只控制预算）

- **每单元 1 卡**（N=1 固定）：generator 输出恰好 1 张锚定类型卡；延续 v2
  live 验证过的“一生成单元一张卡”语义；乘法放大消除；批部分成功歧义同步消除。
- 单元预算公式保持 V4 可测口径：`每章基础单元预算 3 × 密度系数`（COMPACT=1 /
  BALANCED=2 / EXTENSIVE=3）→ 2 章 6/12/18。预算语义 = **上限**（PRD
  5.4.1“不代表固定卡片数量”）：planner 输出可少于预算（内容不足），超限按 priority
  截断。
- 只能承诺**预算上限**满足 COMPACT < BALANCED < EXTENSIVE；Planner 允许少产出，
  因此不同任务实际单元数不承诺严格单调，禁止在验收中把随机输出数量当成确定性关系。
- 预算与真实页数/字符数解耦（真实文本只影响规划输入与引用，不影响预算公式）。这是
  v2.1 为延续 V4 口径而保留的 MVP 产品上限，不等同于按教材篇幅计算的完整覆盖率；
  “按内容长度动态预算”登记为后续候选。

### 3.5 难度配额算法（决策拍板：代码计算并校验）

**三层确定性分配，全部代码计算，最大余数法（largest remainder），固定顺序消除随机性**（配额并列余数时按 BASIC < UNDERSTANDING < APPLICATION、章序、组序）：

1. **任务总配额**：任务总预算（章节数 × 3 × 密度系数）× difficulty_ratio，最大余数法取整到总和 = 总预算。例：预算 6、40/40/20 → 3/2/1（6×0.4=2.4→2、2.4→2、1.2→1，余数 0.4/0.4/0.2，缺额 1 补给余数最大者，并列取 BASIC）。
2. **章配额**：每个难度的总配额按章确定性分发（按各章预算占比，最大余数法）。
3. **子配额**：超长章节拆多次调用时，按各规划分组的 `char_count` 占比分配子配额（最大余数法）。

约束：每次调用只拿自己的子配额（不得把整章配额重复发给每个调用）；Planner 若对某
难度超配额，代码按该难度内 `priority`（并列按原数组顺序）确定性截断，不因“多给了合法
单元”重试整次调用；实际输出允许少于配额；实际难度分布记录观测（不强制补满）。

## 4. 按文件页码持久化文本（`text_chunks`）

### 4.1 持久化模型

- scanner PARSED 时完整解析每页文本；新表 `text_chunks` **一页一行**，字段为：
  `chunk_id` PK、`file_id` FK、`page_number`、`char_count`、`content_sha256`、
  `content` TEXT、`created_at`，并加 `UNIQUE(file_id, page_number)`。
- `text_chunks` 不保存 `chapter_id`，不在扫描阶段按章节切块。章节名称和页码可在
  PARSED 后修改，页文本不随章节编辑、删除而重建；删除 PDF 时按 `file_id`
  `ON DELETE CASCADE` 清理全文页数据。
- `chunk_id` 由服务端按 `(file_id, page_number, content_sha256)` 确定性生成；重解析先
  清理该 file_id 的既有页文本再重建，同一 PDF 内容的页标识稳定。
- AC-08“文本样例不落库”改准为：完整页文本是功能数据，允许写入 `text_chunks`；
  完整 PDF 文本、完整 Prompt、原文样例仍禁止写入日志、请求审计和调用账本。

### 4.2 规划时冻结章节最新页码并选页

- `POST /tasks` 仍校验章节归属，并把当时的 Chapter 对象写入
  `tasks.selected_chapters`，用于保留用户请求和返回 PENDING 任务视图；这只是**创建时
  快照**，不是最终规划输入。
- 首个规划 worker 通过 §6.1 CAS1 抢占后，在同一个短事务内按快照中的 chapter_id
  重新读取 Chapter 当前最新的 `name/start_page/end_page`，覆盖
  `tasks.selected_chapters` 后再提交。该提交时刻形成**规划快照**：抢占前发生的章节页码
  修改会进入本任务，抢占提交后的修改不再影响本任务；孤儿恢复不得再次刷新。
- 若任一所选章节在首次规划抢占前已删除或已不属于该 PDF，不得悄悄沿用旧页码；任务
  直接 FAILED（`failure_stage=PLANNING`，使用既有兜底错误码并在内部原因中区分
  `CHAPTER_SNAPSHOT_STALE`）。
- PLANNING 严格按最终规划快照，从 `text_chunks` 查询
  `file_id = task.file_id AND page_number BETWEEN start_page AND end_page`；再按
  `planner_max_input_chars` 将连续页动态组成规划分组。章节不再持有预生成分片。
- 每次 Planner 调用只暴露本组页文本及对应 `chunk_id`；输出
  `source_chunk_ids` 必须是**本次调用页集合**的子集，而不是整个文件集合的任意子集。
- 章节范围内所有页均无有效文本：该章不发 Planner 请求，作为成功空结果进入
  §6.4；某些页为空不影响其他有文本页参与规划。

## 5. LLM 资产与输出契约

### 5.1 版本布局（保留 v1/v2，审计链完整）

| 资产 | 现状 | 新版本 |
| --- | --- | --- |
| `prompts/` planner | v1（死资产） | `prompts/v3/planner.md`（激活） |
| generator | v1/v2（v2 仅包装修复） | `prompts/v3/generator.md`（继承 v2 包装指令） |
| rewrite | v1 | `prompts/v3/rewrite.md`（内容升级） |
| `rubrics/` main + scoring-prompt | v1（scoring 死资产） | `rubrics/v2/rubric.md` + `scoring-prompt.md`（正式激活，分别进入 manifest） |
| `schemas/` card | v1（保持） | 新增 `schemas/v2/planner-output.schema.json`、`scoring-output.schema.json` |

- manifest：generator v2→v3、planner v1→v3、rewrite v1→v3、rubrics.main v1→v2，
  新增 `prompts.scoring`（路径指向 `rubrics/v2/scoring-prompt.md`）、
  `schemas.planner_output`、`schemas.scoring_output` 三个显式入口；CHANGELOG 追加。运行时禁止
  通过相对路径绕过 manifest 直接读取 scoring prompt。
- 红线 5：structure-contract 3.7/8.5 的版本观测同步。Batch 继续记录生成调用的
  card-schema 版本；`llm_call_attempts` 按调用记录具体的 asset name/version，避免用一个
  `schema_version` 混写 card v1、planner-output v2、scoring-output v2。

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
- `schemas/v2/planner-output.schema.json` 先校验结构（`source_chunk_ids` 至少 1 项且不得
  重复）；代码层（`services/generation/planner_validator.py`，仿 schema_validator 定式）
  校验来源与锚定值，随后按 §3.5 子配额确定性截断；`source_chunk_ids` 必须 ⊆ **本次
  调用提供的页集合**，单元引用页数/总字符数不得超过生成输入上限。通过后服务端按
  `page_number` 规范化来源顺序，保证兼容投影的首项确定。截断后的结果才写入
  `normalized_result`。
- Prompt 只接收代码算好的子配额，不再要求 LLM理解或维持 COMPACT/BALANCED/EXTENSIVE 的相对数量。Prompt 描述三档认知难度的规划形态（BASIC 原子知识点 / UNDERSTANDING 理解主题 / APPLICATION 开放性问题或场景判断题）和 §3.3 组合规则（判断题不可事实换皮、须可二值、explanation 给依据）。

### 5.3 Generator 输出契约

- 每单元一次调用；输出 `{"cards": [恰好 1 张卡]}`（包装指令继承 v2）；输入 = 学习目标 + 锚定难度/卡型 + 分片文本 + schema。
- 代码校验：卡型 = 锚定、数量 = 1；非法 → 批次重试预算（现有语义）。
  `target_difficulty` 是生成输入和服务端落库锚点，不要求模型在 card JSON 中回传；
  card schema v1 不含该字段，难度是否真正匹配由 Rubric 观测，不能伪称结构校验能够判断。
- `card.schema.json` v1 保持（单卡校验）。

### 5.4 Scoring 输出契约

```json
{"scores": [{"generation_item_id": "...", "evidence_score": 2, "correctness_score": 3, "difficulty_score": 2, "learning_value_score": 2, "rubric_total_score": 9}]}
```

- `schemas/v2/scoring-output.schema.json` 校验；代码层额外保证返回
  `generation_item_id` 集合与本次请求集合完全相等、无重复/无越权 ID，四维各 0~3，
  `rubric_total_score` 必须等于四维之和。缺项、多项或总分不一致时整次评分记 FAILED，
  不落部分分数。
- 基础类（BASIC/UNDERSTANDING）合批时多卡一次调用（数组）；应用类逐卡。

### 5.5 输出校验层

`planner_validator.py` / `scoring_validator.py`（仿 `schema_validator.py` 定式）：加载
manifest schema 资产 + 代码规则（数量/锚定/来源/分数范围）。Generator 复用
`validate_card`（单卡）并校验卡型与数量；难度由服务端落规划锚定值，语义匹配由 Rubric
观测。

## 6. 任务状态机（PLANNING 阶段）

### 6.1 状态与抢占

```
POST /tasks → status=PENDING, stage=PLANNING（创建即返回，幂等保持）
```

**规划 worker**（新入口，条件更新 CAS，并发单执行者）：

- CAS1（首次接管）：开启短事务，先执行
  `UPDATE ... SET status='RUNNING', started_at=COALESCE(started_at, now), updated_at=now WHERE status='PENDING' AND stage='PLANNING'`；rowcount=1 的 worker 才能继续。在提交同一事务前，
  按 §4.2 重新读取所选章节最新值并覆盖 `selected_chapters`；章节已失效则在该事务内转
  FAILED。任务从 PENDING 变为 RUNNING 与最终规划快照必须原子提交，不能留下“已接管但
  页码未冻结”的中间状态。
- CAS2（孤儿恢复）：`UPDATE ... SET updated_at=now WHERE status='RUNNING' AND stage='PLANNING' AND updated_at < now - orphan_timeout_minutes`（rowcount=1 才接管）。接管后先把本任务遗留的 STARTED 调用置 UNKNOWN，再按账本恢复。
- **普通 worker 不得直接扫描并执行所有 RUNNING 规划任务**（只走 CAS）。

Task 契约同步增加 `stage=SCORING`；`failure_stage` 同步增加 SCORING，用于评分 worker
自身的不可恢复基础设施/数据库错误。单次评分 LLM 失败仍按 §8 非阻塞处理，不把任务标成
FAILED。

**生成 worker**（现有 executor）：扫描 `RUNNING AND stage=GENERATING`（加 stage 条件，避免与规划中任务冲突）；批次级抢占沿用 V5B（PENDING/FAILED → PROCESSING 条件更新）；RUNNING+GENERATING 孤儿由扫描即处理 + resume 接口（现有语义）。

### 6.2 规划执行

1. 规划选页：按 CAS1 已冻结的最终 `tasks.selected_chapters` 规划快照查询该文件页文本；
   章节有效文本总字符数
   ≤ Settings `planner_max_input_chars` 时一组一次调用，超限则按连续页累计字符拆组。
   分组边界、页 ID、页 `content_sha256`、子配额、prompt/schema 版本共同计算
   `input_fingerprint`。
2. 组数上限：Settings `max_planner_groups_per_task`（全局硬上限，§10）；超限 → 任务
   FAILED（§6.3）。同一任务恢复时必须复用已确定分组；若 fingerprint 与账本不一致，
   不得错误复用旧结果，任务以规划输入漂移失败并记录内部错误。
3. 每次真正发出调用前，先在短事务中重新读取 Task；只有仍为
   `RUNNING + PLANNING` 才能刷新心跳并插入
   `llm_call_attempts(status=STARTED)` 后 commit，随后在事务外调用 LLM。任务已被 cancel/
   pause/转移则立即停止，不得再付费调用。`STARTED` 即占用一次调用/重试预算。
4. 调用成功后校验 schema、本组页引用和锚定，并按子配额确定性截断；合法结果规范化后
   写入该调用行的 `normalized_result` 并置 SUCCESS。失败置 FAILED；进程在调用期间崩溃
   留下的 STARTED 在孤儿接管时置 UNKNOWN，UNKNOWN 仍计入预算。
5. 恢复时，operation_key + input_fingerprint 已有 SUCCESS 的组直接复用
   `normalized_result`，不得重复调用；无成功结果且剩余尝试预算 > 0 才创建下一 attempt。
6. 全部组结束后：**跨调用去重**——指纹 = 规范化后的
   `(learning_objective, target_difficulty, card_type, source_chunk_ids)`（页列表按
   `page_number` 排序；同一页
   可合法产生多个学习目标，不得按 source_chunk_ids 单独去重）；按章序、组序、组内
   priority 分配全局 priority。
7. 最终短事务内重新从 DB 刷新 Task，并以条件更新
   `WHERE status='RUNNING' AND stage='PLANNING'` 完成：写 units（含新列）+
   `plan_batches`（1 单元 1 批 + generation_unit_id）+ 任务 `stage=GENERATING` + 实际难度
   分布。条件不成立（已 cancel/被其他状态转移）则整事务回滚，不得信任会话 identity map。

### 6.3 错误分类与重试（账本为权威）

| 类别 | 判定 | 处理 |
| --- | --- | --- |
| Key 错误 | 解密失败 / 上游 401 → API_KEY_UNAVAILABLE | 任务 FAILED + failure_stage=PLANNING，不重试 |
| 上游暂时失败 | 适配层返回 `retryable=true`（429 / 5xx / 网络） | 该组重试（预算 2 次重试 = 3 次尝试，账本全部 STARTED/SUCCESS/FAILED/UNKNOWN 尝试计数）→ 超限组 SKIPPED |
| 输出非法 | JSON/schema/本组页引用/锚定（单纯超配额按 §3.5 截断，不属于此类） | 同上游（预算内重试）→ 超限组 SKIPPED |
| 硬上限 | 组数 > `max_planner_groups_per_task` | 任务 FAILED + failure_stage=PLANNING + error_code=GENERATION_FAILED（复用兜底码，日志注明） |

### 6.4 空单元语义（决策拍板：有条件 COMPLETED(0)）

- **全组成功但合法单元 0 个**（含无文本层章节）：COMPLETED，
  `total_batch_count=0`、`completed_batch_count=0`、`generated_card_count=0`、
  `completion_reason=NO_GENERATION_UNITS`（tasks 新列）——业务合法结果（PRD 5.6“无有效
  学习内容返回空数组”）。
- **全部规划组因上游/输出错误失败**：FAILED + failure_stage=PLANNING（不伪装成业务空结果）。
- **部分组失败、部分成功**：成功组继续生成；`skipped_planning_group_count`（tasks 新列）记录跳过组数。

### 6.5 cancel / resume / 删除保护

- cancel 覆盖 PENDING/RUNNING（含 PLANNING/GENERATING/SCORING）；PENDING 任务由 worker
  自动接管，不需要用户 resume。resume 覆盖 PAUSED 及心跳超时的 RUNNING+PLANNING /
  RUNNING+GENERATING / RUNNING+SCORING，均使用条件更新防并发接管。
- V1 删除保护（非终态 PENDING/RUNNING/PAUSED 引用 → 409 TASK_IN_PROGRESS）已含 PENDING，无需改动。

## 7. 批关联与观测

- Batch 加列 `generation_unit_id`（FK knowledge_points）；`plan_batches` 按单元建批（每单元一批、batch_index=1..N、显式外键）；**删除 offset 反推逻辑**（batches.py 按 `(batch_index-1)*batch_size` 取 kp 的代码）。
- 新批次代码保证 `generation_unit_id` 必填，并增加
  `UNIQUE(task_id, generation_unit_id)`；迁移列为 NULL 仅用于兼容旧批次。
- 每单元 1 卡 → 部分成功歧义消除：SUCCEEDED = 恰好 1 张合法卡；0 张 → 重试/SKIPPED（现有语义不变）。
- 观测字段语义文档化：`coverage_rate` = 该单元是否产出合法卡（0/1），不再恒定 1.0；distribution 字段为单值。quality-summary 按下述 §8/§9 新分母与归因规则调整，不沿用旧实现原样聚合。
- `Card.target_difficulty` 由规划锚定落库（`_record_rubric` 轮换删除；`target_difficulty` 不再在评分时补写）。
- `Batch.retry_count`、Batch token/版本列保留为生成阶段兼容投影；生成调用的尝试数、
  token 和资产版本以 `llm_call_attempts` 为权威，Batch 投影必须由同一次调用结果同步写入，
  不得形成第二套重试预算。

## 8. Scoring（独立 SCORING 阶段 + 分层抽样）

- **独立阶段**：GENERATING 批循环全部完成 → 任务 `stage=SCORING`（status=RUNNING，心跳/孤儿沿用）→ 评分完成 → COMPLETED。理由：合批跨批（多单元一次调用），批内立即评分无法合批；抽样需要完整单元列表（规划后确定）。进入 SCORING 时卡片已经可读；前端应显示“卡片已生成，质量统计处理中”。cancel 停止后续评分但保留全部已生成卡。
- **预先分层抽样**：规划完成后按（章节 × 难度 × 卡型）分层产生候选评分单元，调用上限 Settings `max_scoring_calls_per_task`（默认 60，约束实际尝试数）。层内使用 `sha256(task_id + generation_unit_id)` 排序进行确定性抽样，不按 priority 只取高优先级内容，避免质量样本系统性偏高。
- **组批规则**：BASIC/UNDERSTANDING 单元按章、难度、卡型合批（多单元一次评分调用）；APPLICATION 每单元单独调用（每单元 1 卡 = 每卡一次）。每个合批同时受 `scoring_max_cards_per_call` 与 `scoring_max_input_chars` 限制，超限继续拆组；组批后调用数 > 上限时按各层配额和确定性哈希缩减。
- **评分输入**：每张卡必须同时携带卡片内容、生成单元的学习目标/锚定难度/卡型及其
  引用页文本；同一调用内重复页只传一次。`scoring_max_input_chars` 计算卡片、规则和去重后
  原文的完整输入，不能只统计卡片字数。否则 evidence/correctness 评分没有可靠依据。
- 评分分组与 operation_key 完全确定：输入指纹包含 generation_item_id、Card.version/内容
  hash、单元锚定、引用页 content_sha256 以及 scoring prompt/schema/rubric 版本。恢复时以
  账本作为已尝试游标，不新增 scoring_batches 表。
- 评分调用按 §9 先持久化 STARTED（stage=SCORING）；短事务内必须再次确认任务仍为
  `RUNNING + SCORING`，否则不发请求。失败不重试、不阻塞卡片入库，账本记
  FAILED/UNKNOWN，任务继续完成。STARTED/FAILED/UNKNOWN 都占用调用上限，避免崩溃恢复
  后突破 60 次。
- 回写评分前重新读取卡片版本；本组任一卡片版本/内容与请求指纹不一致（例如用户在
  SCORING 期间编辑）时，整组结果记 FAILED（内部原因 `STALE_SCORING_INPUT`）且不写旧分数，
  不重试。评分阶段最终转 COMPLETED 也必须使用
  `WHERE status='RUNNING' AND stage='SCORING'` 条件更新，不能覆盖并发 cancel。
- 评分结果校验（scoring-output schema + 分数范围）后落 Card 5 字段 + 批次质量字段（`_record_rubric` 改造为 LLM 输出解析 + 锚定难度，删除轮换）。
- Rubric 观测组合规则（§3.3）：场景判断题的"事实换皮/依据缺失"由 correctness/evidence 分数反映，不做语义拦截。
- quality-summary 的各评分均只以对应字段非 NULL 的卡为分母，新增
  `eligible_card_count`、`scored_card_count`、`sampling_rate`；未被抽中的 NULL 不得按 0 分
  计入。difficulty 分组通过 `Batch.generation_unit_id → GenerationUnit.target_difficulty`
  归因，使生成失败、没有 Card 的 coverage=0 批次仍进入正确难度组。
- rubric v1/v2 分数不得无标识混算：聚合结果至少返回 `rubric_version`，查询窗口同时包含
  多版本时按版本拆组或要求显式过滤。

## 9. `llm_call_attempts` 调用账本（新表）

所有 LLM 调用记账，**重试预算、调用上限与全阶段 token 的权威**（Task 上不设冗余计数列；planner_attempts 单列不足以表达多章多分组，废弃该思路）。任何外部 chat 调用必须先有已提交的 STARTED 行：

| 列 | 说明 |
| --- | --- |
| call_id | PK |
| device_id | 数据归属 |
| scope_type / scope_id | TASK / CARD；任务链路 scope_id=task_id，单卡重写 scope_id=card_id |
| task_id | FK，可空；手动/导入卡重写没有生成任务 |
| stage | PLANNING / GENERATING / SCORING / REWRITE |
| operation_key | planning 含 chapter/group/input fingerprint；generating 含 batch_id；scoring 含确定性 group key；rewrite 含 card_id/card_version/Idempotency-Key hash |
| attempt_no / input_fingerprint | 同一操作的第几次实际尝试；输入身份（不保存完整 Prompt） |
| model / prompt_name / prompt_version | 实际值与资产名/版本 |
| schema_name / schema_version / rubric_version | 本调用实际使用的资产；不适用则 NULL |
| cache_hit / cache_miss / output_tokens | usage 原样 |
| http_status / duration_ms | 观测 |
| status | STARTED / SUCCESS / FAILED / UNKNOWN |
| error_code | 失败类别 |
| normalized_result | 仅 PLANNING 成功时保存通过校验的规范化 units JSON；不保存原文、完整 Prompt 或原始模型响应 |
| created_at / finished_at | 调用占位与结束时间 |

- `input_fingerprint` 按阶段覆盖真实语义输入：PLANNING 见 §6.2；GENERATING 包含单元
  学习目标/锚定/有序页 ID 与 content_sha256 及资产版本；SCORING 见 §8；REWRITE 包含
  Card.version、原内容 hash、用户要求及资产版本。完整原文和完整 Prompt 不进入指纹载荷或
  账本。
- 唯一约束：`(scope_type, scope_id, stage, operation_key, attempt_no)`；同一
  `operation_key + input_fingerprint` 最多一个 SUCCESS。
- 重试判定：该 operation_key 的 STARTED/SUCCESS/FAILED/UNKNOWN 尝试总数达到预算 →
  不再发请求。孤儿 STARTED 转 UNKNOWN 并计数；不能仅统计 FAILED。
- 外部调用返回并完成校验后，领域写入与调用终态必须在同一事务提交：GENERATING 为
  Card/Batch/GenerationUnit + ledger SUCCESS，SCORING 为 Card/Batch 评分 + ledger SUCCESS，
  REWRITE 为 Card 新版本 + ledger SUCCESS。提交失败时保留 STARTED，恢复后按 UNKNOWN
  处理；禁止出现账本已 SUCCESS 但业务结果未落库。
- Scoring 上限：账本 stage=SCORING 的全部尝试数 ≤ 上限（抽样预保证 + 调用前账本
  条件校验）。
- Rewrite 的 operation_key 必须区分同一卡片的多次用户请求；不得用
  `rewrite:{card_id}` 让历史失败影响后续合法重写。
- 8.3 指标（llm_requests_total 等）沿用 llm_metrics 上报，账本是持久化视图。
- 成本口径：账本是 Planner/Generator/Scoring/Rewrite 总 token 的唯一来源；Batch token
  仅是 GENERATING 的兼容投影。现有 quality-summary 中 `cost_estimate` 明确标注为
  **generation-stage only**，不得冒充全链路成本；全链路成本按账本分 stage 汇总，禁止
  将 Batch 投影再次相加造成双计。

## 10. 估算接口删除与全局硬上限

**删除**（用户拍板）：`/tasks/estimate` 路由、CostEstimateRequest/Response schema、`token_estimator.py`、openapi 路径与组件、structure-contract 8.4 事前估算能力 / 6.4 接口行、相关测试。**保留** `cost.py`（按原始 token 换算金额）；Batch/quality-summary 的既有成本字段仅表示生成阶段，完整成本按 §9 账本分 stage 汇总。`planning.py` 整体重构为单元预算计算（knowledge_point_count 删除）。

这是前端破坏性变化，实施时**直接修改实际 Android 前端代码 `frontend-app/Front/`**：
删除创建任务前的
`POST /tasks/estimate` 调用与价格区间 UI；增加 PLANNING / GENERATING / SCORING、
`NO_GENERATION_UNITS`、部分规划跳过的展示，并验证 SCORING 时卡片已可访问、取消评分不删卡。
按用户决定，不新增或更新 `docs/frontend/handoff/*`；前后端机器契约仍以 OpenAPI 为权威。
本工作包只产生本地代码改动与验证结果，不 fork、不 push、不创建远端仓库；用户验收后再把
前端 Git 历史 fork/推送到其指定的 GitHub 仓库。

**新增全局硬上限**（Settings，删除估算前必须落地，防"每章上限随章节数无限增长"）：

- `max_generation_units_per_task`（默认 300）：`POST /tasks` 根据章节数与密度算出的任务
  预算超过上限时直接返回 `VALIDATION_ERROR`，不创建任务、不调用 Planner；Planner 合法
  输出经子配额截断后不可能突破该上限。
- `max_planner_groups_per_task`（默认 30）：规划分组数超限 → 任务 FAILED；每组最多
  3 次实际尝试，因此 Planner 的任务级外部调用数硬上界为 90，账本逐次执行前继续校验。
- `max_scoring_calls_per_task`（默认 60）：评分组批完成后若仍超限，按 §8 的确定性
  分层抽样继续缩减，实际发起的评分调用不得超过上限。
- `max_source_pages_per_unit` / `generator_max_input_chars`：限制单个生成单元引用页数和
  Generator 原文输入，避免 Planner 合法但下游上下文超限。
- `scoring_max_cards_per_call` / `scoring_max_input_chars`：限制合批评分大小；拆分后的实际
  调用仍受 `max_scoring_calls_per_task` 控制。

## 11. 数据库迁移（0003）

| 表 | 变更 |
| --- | --- |
| `knowledge_points` | + `target_difficulty` String NULL、+ `card_type` String NULL、+ `source_chunk_ids` TEXT NULL（旧数据无值，新数据代码保证必填）；旧 `source_chunk_id` 保持 NOT NULL，新数据写首个页 chunk_id 兼容投影 |
| `batches` | + `generation_unit_id` String NULL（FK knowledge_points.knowledge_point_id）；新数据保证必填并加 `UNIQUE(task_id, generation_unit_id)` |
| `tasks` | + `completion_reason` String NULL、+ `skipped_planning_group_count` Integer NOT NULL DEFAULT 0 |
| `text_chunks`（新表） | 一页一行：chunk_id PK、file_id FK ON DELETE CASCADE、page_number、char_count、content_sha256、content TEXT、created_at；`UNIQUE(file_id, page_number)`，无 chapter_id |
| `llm_call_attempts`（新表） | 见 §9；task_id 可空；索引 `(device_id, created_at)`、`(task_id, stage, operation_key)`；attempt 唯一约束 |

空库 upgrade/downgrade/upgrade 往返实测；`alembic check` 零漂移。

## 12. 契约同步

| 位置 | 改动 |
| --- | --- |
| structure-contract 3.10 | ReviewState difficulty 描述 0~10 → **1~10**（对齐 database-design/ORM CHECK） |
| structure-contract 6.10 | 补 quality-summary 分组键定义：model = batch.model；pdf = task.file_id；**difficulty = Batch.generation_unit_id 对应单元的 target_difficulty**（不能只依赖 Card，否则 SKIPPED 批次丢失）；补评分样本分母、sampling_rate、rubric_version 与 generation-stage-only 成本口径 |
| structure-contract 3.5/3.6 | `selected_chapters` 两阶段语义（PENDING 为创建快照、首次 PLANNING 抢占原子刷新并冻结）+ 生成单元概念（3.1）、双维锚定（3.2）、组合规则（3.3）、密度预算与配额（3.4/3.5） |
| structure-contract 3.7/8.5 | 批=单元观测语义（§7）、llm_call_attempts 账本（§9）、scoring 阶段；各调用记录具体 prompt/schema/rubric asset name + version |
| PRD 5.4.1 | 密度 = 单元预算（上限），删除"密度影响知识点数量"的可测口径表述 → 预算公式口径 |
| PRD 5.6 | 生成单元概念、card_type/target_difficulty 锚定、综合应用=开放性问题/场景判断题（非原子聚合） |
| PRD 5.7 | 题型由规划锚定（不再生成期自动选择）；"内容严格依据原文分片"由 text_chunks 输入落实 |
| database-design | 2.5 tasks（含 selected_chapters 两阶段语义及 SCORING 枚举）/ 2.6 knowledge_points / 2.7 batches 列变更 + 新表 text_chunks（一页一行、与章节解耦）、llm_call_attempts |
| openapi | KnowledgePoint/Batch/Task schema 更新（stage/failure_stage 增加 SCORING，selected_chapters 注明两阶段语义）、`/tasks/estimate` 删除、CostEstimate 组件删除 |
| DeepSeek 错误契约 | 模型/thinking/JSON object 不变；适配层向服务层区分 401 非重试与 429/5xx/网络 retryable |
| 前端源代码 | 直接修改 `frontend-app/Front/`：删除 estimate 调用/价格 UI，接 PLANNING/GENERATING/SCORING、空结果和部分跳过展示；不维护 `docs/frontend/handoff/*`，不执行远端 fork/push |
| 红线 5 | manifest ↔ structure-contract 版本一致 |
| R-03 | 本工作包覆盖（**PLANNED**——完成本地实现与守卫后仍不关闭，须通过 §13 的受控真实三链路 canary 才能改 RESOLVED） |

## 13. 测试与验收

四工具全绿 + 守卫全绿（schema↔openapi、ORM↔database-design、错误码↔契约、manifest↔运行时版本）。

新增测试：

- 迁移 0003 往返；text_chunks 每页持久化、确定性 chunk_id、重解析幂等、PDF 删除级联；
  章节页码修改不重写页文本。任务 PENDING 期间修改章节 → 首次规划使用新页码；CAS1
  提交后再修改 → 该任务继续使用已冻结规划快照；孤儿恢复不重复刷新；抢占前章节被删除
  → FAILED 且不发 Planner 请求。
- 配额算法：预算 6 / 40/40/20 → 3/2/1 确定性；章分发、子配额（char_count 占比）、
  并列余数固定顺序；Planner 对单一难度超配额时按 priority 稳定截断且不重试。
- 规划状态机：CAS1（PENDING→RUNNING）、CAS2（孤儿接管，非孤儿拒绝）、取消（最终
  条件更新失败则整事务不写 units）、账本调用前 STARTED、调用中崩溃→UNKNOWN、成功结果
  持久与恢复复用、尝试预算不重置、input fingerprint 漂移拒绝、空单元三分支
  （NO_GENERATION_UNITS / FAILED+PLANNING / 部分成功+skipped 计数）、硬上限 FAILED。
- 分批规划：分组拆分（char 预算）、跨调用指纹去重（同分片多目标合法、重复指纹去重）、全局 priority 分配。
- 锚定校验：卡型=锚定、数量=1、来源 ⊆ 本调用页集合、单元来源页数/字符数上限；
  target_difficulty 由服务端锚定落库、Rubric 观测匹配度，不要求 card JSON 回传。
- Scoring：按层确定性哈希抽样、合批/逐卡、卡片+锚定+引用页输入、去重后输入大小拆组、
  上限缩减、返回 ID 集合与总分守卫、调用前 cancel 守卫、SCORING 阶段状态机、条件完成、
  Card.version 漂移拒绝、失败不阻塞；NULL 不按 0 分，评分分母/sampling_rate 正确，SKIPPED
  批次仍按单元难度计 coverage。
- llm_call_attempts：全阶段调用前占位、attempt 唯一约束、孤儿 UNKNOWN、Planner 规范化结果
  恢复、Rewrite 多次请求互不串预算、task_id 为空卡重写、全阶段 token 与 Batch 生成投影不
  双计；401/429/5xx 分类符合 §6.3。
- 估算删除：后端路由/schema/token_estimator 无引用；`frontend-app/Front/` 源代码不再
  调用 estimate、不展示价格区间，并正确展示
  PLANNING/GENERATING/SCORING/NO_GENERATION_UNITS。
- V4/V5A/V6 测试更新：fake 规划 → mock planner 契约断言；`_record_rubric` 轮换删除 → 锚定落库。

完成口径分两级：四工具/守卫/模拟验收通过只记
`LOCAL_IMPLEMENTATION_DONE`；至少完成一次受预算限制的真实 Planner → Generator →
Scoring canary，验证三类 JSON 输出与账本 token 后，才能记 `PRODUCTION_VALIDATED` 并将
R-03 改为 RESOLVED。更大样本 live 报告可放后续 R 级工作包。

## 14. 登记与风险

- R-03：本工作包覆盖（PLANNED）；仅本地工具/Mock 验收后仍保持 PLANNED，受控真实三链路 canary 通过后才 RESOLVED。
- 加密改造（每用户密钥/信封加密）：后续工作包。
- 每单元 2/4 卡（练习深度）：PRD 变更候选，另行工作包。
- R-17（SQLite 单写者）不变：规划 LLM 调用在事务外，长调用不持写锁；评分阶段同。
- Planner 合法规范化结果写入调用账本，恢复时复用；禁止只暂存进程内后在崩溃时重复付费调用。账本不保存完整原文、Prompt 或原始响应。
- 场景判断题语义依赖 Prompt + Rubric 观测，不做代码语义拦截（登记为观测性约束）。
- 每章固定 3×密度预算是 v2.1 延续 V4 的上限口径，不按篇幅衡量完整覆盖；后续如改为按 char/page 动态预算，属于 PRD 数量语义变更。
