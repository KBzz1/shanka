# AGENTS.md

unit 层：`domain/`、`app/schemas/` 纯逻辑单元测试。

- 覆盖纯逻辑：状态机、FSRS 排程计算、枚举、schema 校验；不触碰 DB/网络/文件等外部依赖。
- 易碎行为（幂等同事务、级联删除、resume 并发）不属本层，见 integration。
- 命名规范：`test_<模块>_<行为>`。
