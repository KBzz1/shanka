# AGENTS.md

contract 层：守卫四项一致性（自动化校验一致性红线的验证手段，见 project-structure.md 第 5 章）。

1. `app/schemas/` ↔ `openapi.yaml`
2. `infra/db/` ORM ↔ `database-design.md`
3. 错误码清单 ↔ `structure-contract.md` 第 7 章
4. `localization_key` ↔ 文案资产清单

- 命名规范：`test_<模块>_<行为>`。
