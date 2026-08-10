# AGENTS.md

DeepSeek 调用与 Prompt 组装；按 `agent_evolution/manifest.json` 加载版本化资产。

- 根 AGENTS.md 红线 4：API Key 只出现在本目录调用路径；任何日志、响应、任务明细不得引用明文；`PUT /api-key` 请求体强制掩码；llm 层异常统一脱敏为 `API_KEY_*` / `GENERATION_FAILED`。
- 所用资产版本必须与 `structure-contract.md` 的 `prompt_version` / `schema_version` / `rubric_version` 一致（红线 5）。
