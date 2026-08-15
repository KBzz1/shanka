# AGENTS.md

本工作包待发布版本（v3）：`generator-output.schema.json`、`planner-output.schema.json`、
`scoring-output.schema.json`。planner-output v3 新增 `coverage_tier` 标签并只允许
`DEEP_QUESTION` 难度；generator/scoring-output v3 为随 manifest 对齐的版本提升（结构与
v2 一致）。Card 持久化 Schema 仍使用 v1。与生产校验器和 manifest 原子验收并发布后即冻结；
发布后的修正或演进走新 `vN/` 目录（规则见 `../../AGENTS.md`）。
