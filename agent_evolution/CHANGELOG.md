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
