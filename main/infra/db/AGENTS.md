# AGENTS.md

ORM（`models.py` / `session.py`）与 Alembic 迁移（`main/migrations/`）。

- 根 AGENTS.md 红线 2：ORM ↔ `docs/Architecture/database-design.md` 表结构一致；每张表必须能回溯到资源模型。
- 表结构变更一律经 Alembic 迁移（`main/migrations/versions/`，演进路径见 database-design.md），禁止绕过迁移直接改表。
