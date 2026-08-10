# AGENTS.md

ORM 与迁移（当前骨架，P0 填充）。

- 根 AGENTS.md 红线 2：ORM ↔ `docs/Architecture/database-design.md` 表结构一致；每张表必须能回溯到资源模型。
- 迁移用 Alembic（database-design.md 演进路径），禁止绕过迁移直接改表。
