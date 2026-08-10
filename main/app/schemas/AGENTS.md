# AGENTS.md

请求/响应模型，按资源分文件，与 `../domain/` 模型对应但不混用（边界序列化用 schema，内部数据结构用 domain）。

- 根 AGENTS.md 红线 1：本目录 ↔ `docs/Architecture/openapi.yaml` ↔ structure-contract.md 资源模型三处一致，变更从结构契约发起。
- 新增/删除字段必须同步 openapi.yaml 与数据库表（docs/Architecture/AGENTS.md 防漂移规则 5）。
