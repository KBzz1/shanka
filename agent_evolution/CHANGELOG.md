# agent_evolution 演进日志

## v1（2026-08-10）

- 初始资产：prompts（planner/generator）、schemas（card）、rubrics（main + scoring-prompt）。
- 来源：PRD v2.1（5.6/5.7/5.8/5.9）与结构契约（3.5/3.9）推导的首版草稿，P2-4/P2-5 实现时精修。

## 2026-08-11
- 新增 prompts/v1/rewrite.md（V6 单卡重写，manifest prompts.rewrite v1）；generator/planner 不变。
- **prompts/generator v1 → v2（R1 canary 修复）**：输出指令改为 `{"cards": [单张卡片对象]}`（与 V5A 批次解析器 `parse_cards_json` 的数组包装契约一致；v1 指令输出裸单卡对象导致真实模型响应 0 卡入库）。manifest prompts.generator → v2。

## 2026-08-12

- **prompts/planner v1 → v3**：规划对象升级为生成单元，输出学习目标、目标难度、锚定卡型
  与来源页；加入难度配额、动态来源上限、最小充分引用、少产出不凑数及全运行时字符串的
  Prompt Injection 隔离。数组顺序表达局部优先级，数值 priority 改由服务端分配。
- **prompts/generator v2 → v3**：改为一生成单元一张锚定卡；明确 BASIC / UNDERSTANDING /
  APPLICATION 认知动作、APPLICATION 场景问答/场景判断题规则、QUESTION/TRUE_FALSE 字段
  语义与严格 JSON 包装。新增精炼自然、反模板化、纯文本、分难度软长度目标和 3 个紧凑
  示例。模型仅输出最小语义字段，Card v1 的 front/back 改由服务端确定性投影；证据不足时
  仅允许 `{"cards":[]}` 安全弃权，禁止用常识补原文。
- **prompts/rewrite v1 → v3**：保持题型、核心主题、原命题含义与难度不变；附加要求降为
  表达偏好，禁止借重写引入新事实。补齐两种卡型的字段一致性及注入隔离规则。
- **rubrics/main v1 → v2**：四维 0~3 分改为可操作的绝对评分锚点；细化 item 级来源隔离、
  来源不足与明确错误的区别、三档 × 两卡型难度降档和 APPLICATION“事实换皮”等边界；
  明确不奖励篇幅并加入 3 个紧凑校准例。
- 新增 **prompts/scoring v2**：支持多卡合批、逐卡独立评分、item 级来源隔离、输入/输出 ID
  守恒和来源页注入隔离。评分模型只输出四维原始分，派生总分由服务端计算。
- 新增 **schemas/generator_output v2**、**planner_output v2** 与 **scoring_output v2**：顶层
  强制 object、禁止额外字段，固定枚举、必填字段、单卡 0/1 包装及评分整数范围；Card
  Schema 继续使用 v1，作为服务端投影后的持久化结构校验。
- manifest 已切换到上述新资产。Planner/Generator/Rewrite/Scoring 的运行时组装必须按各
  Prompt 声明的 XML 标记包裹服务端 JSON 序列化输入；未完成新链路组装前不得将该 manifest
  单独部署到旧执行器。

## 2026-08-16

- **prompts/planner v3 → v4**：规划对象升级为来源接地语义单元，每单元新增
  `coverage_tier` 标签（`CORE` / `IMPORTANT` / `LOW_FREQUENCY`）；运行时输入新增
  `coverage_mode`（`COMPACT` / `BALANCED` / `EXTENSIVE`），覆盖模式选择语义范围而非
  数量；难度配额键改为 `BASIC / UNDERSTANDING / DEEP_QUESTION`（原 APPLICATION 改名，
  结构契约 3.5/3.6）。`DEEP_QUESTION` 只允许 QUESTION 卡型，判断题只属于前两档；
  知识稀疏章节在 EXTENSIVE 下允许围绕同一知识点规划不同学习角度，禁止同义重复。
- **prompts/generator v3 → v4**：难度枚举改为 `BASIC / UNDERSTANDING /
  DEEP_QUESTION`；`DEEP_QUESTION` 只映射开放深问卡（QUESTION），背面为参考思路而非
  唯一标准答案；判断题只属于前两档；软长度表新增 DEEP_QUESTION 行。
- **prompts/rewrite v3 → v4**：补充 `DEEP_QUESTION` 原卡重写规则——保持开放问题开放性
  与参考思路性质，不得改写为“唯一标准答案”式断言。
- **rubrics/main v2 → v3**：难度口径改为 `BASIC / UNDERSTANDING / DEEP_QUESTION`；
  新增 DEEP_QUESTION 评分边界（参考思路不得因多解而扣分、不得伪装唯一答案、必须真实
  承担迁移/权衡/综合）；校准例同步。
- **prompts/scoring v2 → v3**：补充 DEEP_QUESTION 参考思路评分口径，其余不变。
- **schemas/planner_output v2 → v3**：单元必填新增 `coverage_tier`（枚举
  `CORE / IMPORTANT / LOW_FREQUENCY`），`target_difficulty` 枚举改为
  `BASIC / UNDERSTANDING / DEEP_QUESTION`；generator_output 与 scoring_output 随
  manifest 对齐提升到 v3（结构与 v2 一致）。
- manifest 已切换到上述新资产。Planner/Generator/Rewrite/Scoring 的运行时组装必须按各
  Prompt 声明的 XML 标记包裹服务端 JSON 序列化输入；服务端难度键须为
  `DEEP_QUESTION`（Task 7 起规划/配额/分布口径同步改名），历史 `APPLICATION` 值经
  迁移映射（`domain/task.py` DIFFICULTY_V25_MIGRATION）为 `DEEP_QUESTION`。

## 2026-08-31（密度制 V25-D-25/26/27）

- **prompts/planner v4 → v5**：`difficulty_quota`（硬上限、"上限禁凑"）改为
  `difficulty_interval`（`{min, max}` 密度区间，V25-D-25）——区间由章节内容规模推导，
  区间内按实际内容密度取舍：来源充分向区间上部规划，来源稀薄允许低于 min 输出；
  薄内容不注水、富内容不偷工。0/0 语义保持"禁止输出该难度"。
- **prompts/generator v4 → v5**：运行时输入由单一 `<GENERATOR_INPUT>` 改为三区块
  `<GENERATION_SPEC>`（机器规范：learning_objective/target_difficulty/card_type/
  coverage_tier，V25-D-27）+ `<SOURCE_MATERIAL>`（原文）+ `<USER_REQUIREMENTS>`
  （用户自定义偏好）；SPEC 枚举取值为锚定约束，字符串内容仍按不可信数据处理；
  coverage_tier 用于校准详略与角度，不改变难度/卡型锚定。其余规则不变。
- **schemas/planner_output v3 → v4**：字段与 v3 完全相同；数量约束由服务端密度区间
  执行（$comment 记录语义），schema 不约束数量。
- **manifest** 切换到 planner v5 / generator v5 / planner_output v4；scoring/rubric
  保持 v3（四维语义与密度制不冲突）。
- 配套服务端变更（同次）：`quota.py` 密度锚点 {6,12,20}/万字 + 区间推导；
  `planner_validator` 区间上限截断（min 不强制填充）；`knowledge_points.coverage_tier`
  落库（迁移 a3f8d21c9e47）并注入 GENERATION_SPEC。

## 2026-08-31（v6 难度锚定重设计）

- **prompts/planner v5 → v6** 与 **prompts/generator v5 → v6**：针对首轮密度制真实验收
  （任务 435598b1，双裁判盲评）暴露的系统性短板——难度匹配维均分 1.83/3，10/18 卡为
  清单复述或定义换皮——按提示词工程口径重设计三挡位难度规则，其余章节不动：
  - **认知动作操作化**：难度只看"学习者答对此卡必须实际完成的认知动作"；新增降挡检验
    （"定位与题干关键词重合的来源单句、照抄即可答对 → 最多 BASIC"），写完必查。
  - **枚举拆分规则（planner/generator 两侧）**："N 要素及各自职责"式复合枚举目标一次考
    多个原子事实，任何难度锚定下不合格；集合辨识为 BASIC（只问集合）、成员机制按真实
    动作定档、结构意义与成员关系为 UNDERSTANDING。
  - **单问点题干（generator）**：题干只含一个问句或一个判定任务，禁止"以及/分别/
    哪些……哪些"子问题堆叠。
  - **UNDERSTANDING 反换皮**：来源原句已直述的关系链，再问一遍属于 BASIC；合格标准改为
    "答案关键主张需连接至少两个来源信息点或把规则用于新实例"。
  - **DEEP_QUESTION 场景化**：题干必须给出具体、有界的新场景（具体对象/条件/数值/判断
    点），只用来源规则可判定；禁止"应依据什么来决策""请多角度分析"式空壳（实为规则
    复述）。planner 侧同构：无场景与判断点的目标不得标 DEEP_QUESTION。
  - **近失对比示例**：generator 新增示例 4（UNDERSTANDING 近失 vs 合格，同源对照）与
    示例 5（DEEP 空壳 vs 场景化，含 JSON）；planner 静态示例补充规则复述/枚举拆分/
    直述关系链三个判定对。示例域刻意与验收样章（Agent/Harness）错开，防止示例内容
    渗入真实生成。
  - **认知动词表**：BASIC（说出/写出/指出/列举）、UNDERSTANDING（解释/比较/归类/推断/
    说明原因或后果/判断条件）、DEEP（在……场景中判断/权衡/选择并说明依据），对齐
    Bloom 修订版认知过程维度。
  - **分层静默自检**：输出前按挡位各查一项（BASIC 单点、UNDERSTANDING 不可照抄单句、
    DEEP 场景具体有界）。
- **schemas/rubrics 均不变**：planner_output 保持 v4、generator_output/scoring_output/
  rubric 保持 v3——本次为纯提示词资产演进，无结构变更；rubric v3 难度维锚点本就与新
  口径一致（新提示词向 rubric 对齐，而非反向）。
- **manifest** 切换到 planner v6 / generator v6；rewrite 保持 v4、scoring 保持 v3。
- 依据：双裁判盲评量化短板 + LLM 出题研究共识（高阶认知题生成显著弱于低阶回忆题）+
  近失负例对上下文学习的强化效应 + Bloom 修订版动作动词口径。
