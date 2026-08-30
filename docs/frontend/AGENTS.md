# AGENTS.md

前端对接文档：`backend-integration.md`（接入指南：openapi.yaml / structure-contract.md 的使用导览 + 部署环境信息）、`offline-data-layer.md`（客户端离线数据层契约：Room 投影/评分 outbox/请求调度）与 `handoff/` 下的 `handoff-YYYY-MM-DD.md`（联调交接记录，按日期命名）。

- 机器接口权威是 `../Architecture/openapi.yaml`，行为契约权威是 `../Architecture/structure-contract.md`；本目录文档与契约冲突时以契约为准。
- handoff 记录如实写明处理结论与当前状态（不承诺未实现功能），接口字段与行为以线上 `/openapi.json` 为准。
