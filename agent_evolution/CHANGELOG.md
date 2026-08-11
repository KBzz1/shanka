# agent_evolution 演进日志

## v1（2026-08-10）

- 初始资产：prompts（planner/generator）、schemas（card）、rubrics（main + scoring-prompt）。
- 来源：PRD v2.1（5.6/5.7/5.8/5.9）与结构契约（3.5/3.9）推导的首版草稿，P2-4/P2-5 实现时精修。

## 2026-08-11
- 新增 prompts/v1/rewrite.md（V6 单卡重写，manifest prompts.rewrite v1）；generator/planner 不变。
- **prompts/generator v1 → v2（R1 canary 修复）**：输出指令改为 `{"cards": [单张卡片对象]}`（与 V5A 批次解析器 `parse_cards_json` 的数组包装契约一致；v1 指令输出裸单卡对象导致真实模型响应 0 卡入库）。manifest prompts.generator → v2。
