# AGENTS.md

HTTP 出入口：`api/` 路由、`middleware/` 横切、`schemas/` 请求/响应模型、`main.py` 装配。

- 分层 `app → services → infra` 单向（根 AGENTS.md 仓库布局），handler 禁止直接暴露 ORM 对象。
- 新接口流程：先确认 `docs/Architecture/` 契约（openapi.yaml 路径 + structure-contract.md 行为），再实现，保持三处一致（根 AGENTS.md 红线 1）。
