# AGENTS.md

agent 版本化资产（prompt / schema / rubric）。运行时由 `main/infra/llm/` 按 `manifest.json` 加载。

## 演进规则（技术评审级变更）

- 新版本 = 新版本目录（如 `prompts/v2/`）+ 更新 `manifest.json` + 追加 `CHANGELOG.md`，三者同次提交。
- 已发布版本目录（`*/v1/`）禁止原地修改；修正即升级版本。
- `manifest.json` 中 `version` 必须与目录名一致，`path` 指向实际文件。
- 版本号必须与 `docs/Architecture/structure-contract.md` 的 `prompt_version` / `schema_version` / `rubric_version` 一致（根 AGENTS.md 红线 5）。
