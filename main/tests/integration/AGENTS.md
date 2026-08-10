# AGENTS.md

integration 层：`services/` 编排、DB 事务边界。

- 易碎行为必须出现在本层：幂等同事务、级联删除、resume 并发（对应 database-design.md 事务边界）。
- 命名规范：`test_<模块>_<行为>`。
