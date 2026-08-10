# AGENTS.md

用例编排层：decks / generation / pdf / scheduling（FSRS-6）/ stats 各一服务包。

- 业务规则编排在服务层，外部副作用交给 `../infra/`；服务不向 app 暴露 ORM 对象。
- 实现前先确认 `docs/Architecture/structure-contract.md` 行为契约（状态机、幂等、排程、错误码）。
