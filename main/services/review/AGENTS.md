# AGENTS.md

复习用例：到期队列 + 评级事务（review_event 插入 + review_state 快照 + client_event_id 兜底）。

- 事务语义：本模块不 commit/rollback，调用方控制；review_event 与 review_state 更新同事务（database-design §3）。
- 幂等（1.3）：Idempotency-Key 层由 handler 处理；本模块负责 client_event_id 兜底（UNIQUE(user_id, client_event_id) 冲突 → 比对 → 重放/409）。
- FSRS 构造口径经 `../scheduling/scheduler.py` 适配器导出（build_fsrs_card / state_upper），本模块不直接 import fsrs。
