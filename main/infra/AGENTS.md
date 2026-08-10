# AGENTS.md

基础设施：`db/`（ORM 与迁移）、`storage/`（PDF 文件）、`llm/`（DeepSeek 调用与 Prompt 组装）。

- 依赖规则：infra 可依赖 `domain/`，不可反向依赖 app / services（根 AGENTS.md 仓库布局）。
- 外部副作用（网络、文件、数据库）只允许出现在本层；各子目录红线见对应 AGENTS.md。
