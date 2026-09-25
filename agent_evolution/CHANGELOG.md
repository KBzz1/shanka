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

## 2026-09-09（v7 两阶段规划：粗规划主题盘点 + 精规划主题展开）

- **prompts/planner v6 → v7（精规划）**：规划职责拆为两阶段，本资产承载第二阶段——只展开
  服务端分配的主题（`topic_index` 锚定），默认一主题一单元、核心主题可按不同认知动作展开
  2~3 条；新增"紧扣主题、不得漂移到相邻知识点"条款；输出单元含 `topic_index`、不再输出
  `coverage_tier`（由服务端从主题注入，结构上杜绝层级越权）；难度锚定、卡型组合与目标写法
  沿用 v6。
- **新增 prompts/planner_coarse v7（粗规划）**：整章一次性知识盘点，输出主题清单
  `{topics:[{title, coverage_tier, source_chunk_ids}]}`；`topic_interval` 数量目标沿用密度
  锚点；覆盖层级语义表沿用 v6（coverage_mode → 允许层级）；主题粒度硬规则（并列必拆、
  枚举必拆、复述合并）与逐小节/表格/边栏/脚注清点步骤——依据 2026-09-09 消融实验评委
  反馈（原子性与低频覆盖为普遍弱项）强化。
- **schemas/planner_output v4 → v7（精规划输出）**：units 增必填 `topic_index`（≥1 整数）、
  删 `coverage_tier`（服务端注入）；其余字段与 v4 一致。
- **新增 schemas/planner_coarse_output v7**：`{topics:[...]}` 结构，title 2~80 字、
  coverage_tier 三值枚举、source_chunk_ids 1~8 项唯一。
- **依据**：两阶段消融实验（scripts/planning_ablation/report.md，子代理驱动零 API）——
  8/8 cell 命中密度区间，同章三档 25/49/81（旧单阶段架构 19/19 档位无区分）；tier 违规 0、
  重复标题 0、B3 覆盖 0.86~1.00；`density_assessment` 密度自评变体四组对照全部误判 RICH 且
  评委分无改善，**不采纳**，v7 保持纯锚点区间。
- **manifest** 切换到 planner v7 / planner_coarse v7 / planner_output v7 /
  planner_coarse_output v7；generator 保持 v6、rewrite 保持 v4、scoring 保持 v3、card 保持
  v1、generator_output 保持 v3、scoring_output 保持 v3、rubric 保持 v3。

## 2026-09-12（v8 自定义要求执行力 + 反空泛技术锚定；generator v7 输出语言治理）

- **依据**：生产库证据（任务 fac741c5）——`custom_requirements`「生成不要有英文」下
  62 卡中 71% 仍含英文，与无指令基线（74%~90%）几乎无差；同时存在系统性空泛概念卡
  （「用户记忆系统的本质目标是什么？」跨任务出现 2 次，第 1 章 21.1% 正面为"N 个
  原则/目标"式宏观卡）。根因：三个现行 prompt 的信任边界条款把 `custom_requirements`
  统一定性为"不可信数据，不是新指令"，且 generator v6「保留来源关键术语」与显式语言
  指令冲突时无优先级仲裁；规划/制卡两侧均无"聚焦具体技术对象、拒绝宏观定位式卡"的
  通用条款。
- **prompts/planner v7 → v8（精规划）**：信任边界重分类——`custom_requirements` 从
  "不可信数据"改为"认证用户配置的正式输入，内容侧重/术语保留/目标语言必须执行"（仍
  不得改变 Schema/枚举/区间/证据规则、不得引入外部知识）；目标写法新增技术锚定硬规则
  （禁止"X 的目标/意义/价值/定位"式宏观目标，来源只有定位性表述时改锚定具体技术侧面
  或输出 0 条），锚定对象按挡位分化——BASIC 锚定单一原子事实、UNDERSTANDING 锚定
  对象间关系、DEEP_QUESTION 锚定有界场景与判断点（引发权衡与系统性整合，不以单一
  可检索事实为答案）；新增语言传导规则（显式目标语言决定 `learning_objective` 书写
  语言，代码级标识符除外）；主题展开规则新增"内容侧重倾斜展开角度"。
- **新增 prompts/planner_coarse v8（粗规划）**：同款信任边界重分类；新增「技术锚定：
  先具体机制，后宏观定位」小节（优先机制/流程/字段/接口/参数/规则类知识，纯定位性
  段落不单独列主题）并纳入盘点步骤第 3 步；静态示例新增"记忆系统的目标"不合格主题
  对照例。
- **prompts/generator v6 → v7**：信任边界重分类（同上，范围限输出语言/措辞/详略/呈现
  侧重）；新增「输出语言」小节——显式语言指令优先级最高，压过默认语言链与术语保留
  条款，为语言中立的通用机制（不预设任何目标语言），默认语言链保留为无显式指令时的
  兜底；「内容与文风」背面锚定按挡位分化（BASIC/UNDERSTANDING 落具体技术对象，
  DEEP_QUESTION 参考思路贴场景示范「识别约束→组合规则→权衡取舍」推理路径、引发思考
  与系统观念）；输出前静默自检增语言核对项。（当日修订：初稿含中文化译名细则与代码
  标识符白名单，复盘认定系对"不要英文"这一验证探针场景的过拟合——该探针用于暴露
  遵守度弱的问题，并非产品需求——收敛为语言中立条款，示例题干恢复来源术语 "Agent"。）
- **schemas/rubrics 均不变**：planner_output / planner_coarse_output 保持 v7、
  generator_output / scoring_output 保持 v3、card 保持 v1、rubric 保持 v3——本次为纯
  提示词行为演进，无结构变更。
- **manifest** 切换到 planner v8 / planner_coarse v8（prompts/v8/）/ generator v7
  （prompts/v7/generator.md）；rewrite 保持 v4、scoring 保持 v3。
- 配套：生产模型默认值 `deepseek-v4-flash` → `deepseek-flash`（官方 2026-09-10 发布
  V4.1-Flash，旧名仅临时兼容路由；DeepSeekClient 无代码变更，仅 config 默认值与测试
  断言同步）。

## 2026-09-16（v9 新增 chapter_planner：无目录 PDF 的 AI 章节边界规划，V25-D-36）

- **依据**：PRD V25-D-36（显式推翻历史「目录解析失败不提供 AI 猜测兜底」规则）——无目录
  PDF 此前直接 FAILED（PDF_TOC_MISSING）且只能换文件；页文本已在解析期全量落
  text_chunks，具备分段喂给模型识别章节起始点的数据基础。
- **新增 prompts/chapter_planner v9**：全新资产（planner/generator/rewrite/scoring 均
  不变）。文档结构分析师角色：输入一段连续内容页（资料按约 2.4 万字符切段，各段调用
  system 部分逐字节一致以吃自动前缀缓存），只输出**在本段内开始**的章节边界
  `{title, start_page}`；边界判定按可信度排序（显式章节标题 > 无标题的主要部分级主题
  切换 > 前置内容后的正文起点），小节级切换与不确定项宁缺毋滥；段边界规则处理跨段延续
  （本段开头是上一章收尾时不报告）；页码只能引用本段实际出现的页，运行时字符串一律
  不可信。措辞资料类型中立（「内容页/资料」，不出现 PDF），为后续推广到 TEXT/ZIP 预留。
- **新增 schemas/chapter_planner_output v9**：`{"chapters": [{"title"(1~80 字),
  "start_page"(≥1 整数)}]}`；服务端另有确定性校验（start_page 必须落在段内区间、标题
  规范化去重、每段边界数上限），schema 只管结构层。
- **manifest** 新增 prompts.chapter_planner → v9、schemas.chapter_planner_output →
  v9；既有资产版本全部不变（planner/planner_coarse v8、generator v7、rewrite v4、
  scoring v3、card v1、planner_output/planner_coarse_output v7、generator_output/
  scoring_output v3、rubric v3）。
- 配套服务端：`llm_call_attempts.stage` 域新增 `CHAPTER_PLANNING`（scope_type=
  MATERIAL、operation_key=`chapters:{material_id}:{segment_index}`）；章节规划失败 →
  `PDF_AI_CHAPTERS_FAILED`，支持 reparse 重试与整本单章降级（详见 structure-contract
  3.2/6.2）。

## 2026-09-17（v10 章节体系分层原理 + 0 边界引导重试；两书量化评测驱动）

- **依据**：chapter_planning_eval 两书量化证据（deepseek-flash）——样书（"第 N 章"显式
  标题）recall 0.917 / precision 0.355（偏多切）；面试问答笔记（中文序号「一→十一」节头
  + 高频「卡片 NN」条目行）输出 0 边界、静默降级整本单章（recall 0.000）。根因：v9 的
  边界判定为形态枚举（"第 N 章/Chapter N/Part N"），中文序号不在枚举内，且未提供多编号
  体系的分层方法，模型把节头与条目行同级看待后在"宁缺毋滥"约束下整体弃权。
- **prompts/chapter_planner v9 → v10**：边界判定从形态枚举改为**分层原理**——通读后列出
  文档内全部编号标题体系，按编号跨度与重复频次分层，「跨度最大、频次最低」者为章节体系，
  其每个成员标题行必报；高频重复编号行（题号/卡片号/练习号）为条目，明确排除；中文序号
  与阿拉伯/拉丁编号同权。宁缺毋滥改为分级适用（显式编号节头必报；仅无编号主题切换适用）。
  静态示例新增问答/题库类正反例（中文序号节头 vs 题号行），示例措辞保持通用形态以避免
  向评测样本过拟合。schemas/chapter_planner_output 保持 v9（结构不变）。
- **服务端流程（services/chapters/planner.py）**：段返回 0 边界时追加**引导重试**一次——
  同 system、user 附加「章节体系判定原理」引导指令（重新检查编号体系，存在则必须输出），
  独立 operation_key `chapters:{material_id}:{seg}:guided`（不挤占首次预算，账本独立留痕）；
  引导轮仍为空才视为"该段确实无章节结构"，维持 V25-D-36 静默降级语义。
- **manifest**：prompts.chapter_planner → v10；其余资产版本全部不变（planner/planner_coarse
  v8、generator v7、rewrite v4、scoring v3、card v1、planner_output/planner_coarse_output v7、
  chapter_planner_output v9、generator_output/scoring_output v3、rubric v3）。
- **验证结果（chapter_planning_eval 两书回归，deepseek-flash 实测）**：闪卡书 F1 0.000 →
  **0.952**（precision 1.000 / recall 0.909；唯一漏检为同页双节头被确定性页去重合并，属
  页级模型固有限制），多轮重跑稳定；样书 recall 持平（0.917 附近），precision 由 0.355
  降至约 0.24~0.28（多切以 N.N 小节为主），F1 0.512 → 0.377~0.431——**未达"样书 F1 不降"
  门槛**。定性结论：v10 分层原理对 flash 规模模型可执行性有限，样书多切为模型固有倾向，
  提示词层修不动（同措辞两次运行 F1 波动 0.431/0.377，与措辞微调效应同量级）；两书综合
  （0.377+0.952 vs 0.512+0）与产品价值（全弃不可用 → 可用、用户可在确认环节删除多切章节）
  支持采纳 v10。样书 precision 的根治方向是架构层确定性分层（正则抽标题行候选 + 代码按
  跨度/频次分层，模型仅确认），已列为后续工作包候选，不在本提示词版本内。

## 2026-09-25（V25-D-43 问答直通模式新增 qa 资产）

- **依据**：PRD V25-D-43——资料本身已含问题和答案（题库/问答集）时，既有 EXTRACT 链路
  （挖知识点→命题）会丢弃资料已定稿的问答。新增任务配置 `source_mode=QA_DIRECT`，规划
  阶段切换为问答对提取，生成阶段切换为忠实整理，评分/rubric 照常复用。
- **新增 prompts/v1/qa-planner.md**（manifest prompts.qa_planner v1）：题库整理员角色，
  逐对盘点资料已有问答并输出**清单**——题干/陈述保持原义原句，只做提取级清理（题号
  前缀/排版噪音），不命题、不改答案、不合并拆分、不判定对错；判断题（陈述+对/错）输出
  TRUE_FALSE 形态，其余一律 QUESTION；**输出经济硬约束：不回抄答案/解析/对错判定**（制卡
  阶段按出处回原文照录；回抄答案会在长题库时把输出 token 上限撑爆导致 JSON 截断——本条
  为同日真实验收发现输出超限后的 v1 定稿形态）；无问答内容时如实为空。
- **新增 prompts/v1/generator-qa.md**（manifest prompts.generator_qa v1）：题库排版员
  角色，把单个问答对整理成卡面结构——仅格式规范化（字段适配、噪音清理、按来源页恢复
  完整排版），禁止改写/润色/纠错/重新命题；SPEC 携带资料原问题/原陈述（提取级清理后），答案在
  SOURCE_MATERIAL 中定位照录（判断题的对/错标记与解析同样在来源页定位）、定位不唯一即
  弃权；含 `sample: true` 样卡直取条款（从材料开头取第一个完整问答对预览）。
- **新增 schemas/v1/qa-planner-output.schema.json**（manifest schemas.qa_planner_output v1）：
  `{qa_pairs:[...]}` 清单式双形态 oneOf——QUESTION（question）与 TRUE_FALSE（statement），
  均带 source_chunk_ids（1~8 块），**均不含答案字段**（输出经济，见上）；校验器对模型
  仍回抄的 answer/answer_boolean/explanation 键做防御性剥除。
- 既有资产版本全部不变（planner/planner_coarse v8、generator v7、rewrite v4、scoring v3、
  chapter_planner v10、card v1、planner_output/planner_coarse_output v7、
  chapter_planner_output v9、generator_output/scoring_output v3、rubric v3）；EXTRACT 链路
  零改动。生成输出继续复用 generator_output v3（输出形状与既有解析/投影链一致）。
