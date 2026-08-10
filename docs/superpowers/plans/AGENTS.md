# AGENTS.md

实施计划（superpowers:writing-plans 产物），用 `- [ ]` 勾选跟踪进度。

- 执行用 superpowers:subagent-driven-development 或 executing-plans；完成后不删除历史计划，在标题下注明结果。
- 每份计划只细化 `docs/Progress.md` 当前一个工作包；Progress 是唯一范围、依赖、状态和 DONE 事实源，不创建覆盖 F0～R1 的并列总计划。
- 主执行 Agent 负责集成和整包验收；实现 subagent 不修改 PRD、Architecture 或 Progress，只有整包验收通过后由主 Agent 更新 Progress。
