# AGENTS.md

输出 JSON Schema 资产目录：`vN/` 下每文件一个 Schema。Card 持久化 Schema 保持 v1；
Generator、Planner 与 Scoring 顶层模型输出 Schema 位于 v3。当前版本入口见
`../manifest.json` 的 schemas 节。

- 已发布 `vN/` 目录禁止原地修改；演进规则见 `../AGENTS.md`。
