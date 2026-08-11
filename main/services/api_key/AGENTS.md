# AGENTS.md

Key 保存/状态/覆盖规则/脱敏用例（structure-contract 6.2；database-design 2.2）。

- 覆盖规则：仅 AVAILABLE 落库/覆盖（INVALID/INSUFFICIENT_BALANCE 不保存不覆盖——6.2 旧有效 Key 保护）；`get_status` 只返回 DB 状态，不重校验。
- 明文 Key 只存在于调用栈（handler → service → infra/llm crypto），不落库不落日志（根 AGENTS.md 红线 4）。
