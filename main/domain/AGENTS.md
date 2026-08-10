# AGENTS.md

纯领域模型与枚举：`enums.py` 为状态/评级/类型/来源唯一出处，各资源一个模块（ApiKey / Batch / Card / Chapter / Deck / ...），模块注释标注契约章节号。

- 零依赖：不 import app / services / infra 任何内容（根 AGENTS.md 仓库布局）。
- 与 `docs/Architecture/structure-contract.md` 第 3 章资源模型一一对应，字段名以契约为准。
