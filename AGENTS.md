# AGENTS.md

闪卡 App v2.1 后端仓库（契约驱动实现）。依赖方向单向向下：`docs/PRD → docs/Architecture → main/`，实现不得反向驱动契约。

## 仓库布局

- `docs/` — 文档层级：`PRD/V2.1/prd_v2_1.md` 需求权威（做什么、为什么）；`Architecture/` 设计契约集合 + 防漂移规则（见 `Architecture/AGENTS.md`）；`frontend/` 前端对接、`superpowers/` 工作流产物、`Progress.md` 进度（层级规则见 `docs/AGENTS.md`）。
- `main/` — FastAPI 后端（Python >= 3.12）：
  - `app/` HTTP 出入口：`api/` 路由、`middleware/`（Bearer 认证、Idempotency-Key 幂等、统一错误）、`schemas/` 请求/响应模型、`main.py` 装配。
  - `domain/` 纯领域模型与枚举，不依赖任何其他包。
  - `services/` 用例编排：auth / api_key / cards / decks / generation / pdf / review / scheduling（FSRS-6）/ stats / tasks。
  - `infra/` db（ORM 与迁移，`main/migrations/` 为 Alembic 迁移）/ storage（PDF 文件）/ llm（DeepSeek 调用、Prompt 组装）。
  - 分层依赖：`app → services → infra` 单向，均可依赖 `domain/`；禁止在 handler 中直接暴露 ORM 对象。
- `agent_evolution/` — agent 版本化资产（prompt/schema/rubric + manifest.json），`main/infra/llm/` 按 manifest 加载；资产演进 = 新版本目录 + 更新 manifest + CHANGELOG，属技术评审级变更。
- `scripts/` — run.sh / stop.sh（启动/停止，语义见 `docs/Architecture/deployment.md` 契约 4.1）。
- `res/` — 样书 PDF 夹具（只读引用，勿替换，规则见 `main/services/pdf/AGENTS.md`）。

## 一致性红线（改动前先确认下游）

1. `app/schemas/` ↔ `openapi.yaml` ↔ `structure-contract.md` 资源模型，三处一致。
2. `infra/db/` ORM ↔ `database-design.md` 表结构一致。
3. 幂等键、设备 ID 头、错误码格式的实现在 `app/middleware/` 统一，禁止散落各处。
4. API Key 只出现在 `infra/llm/` 调用路径：任何日志、响应、任务明细不得引用明文；`PUT /api-key` 请求体强制掩码；llm 层异常统一脱敏为 `API_KEY_*` / `GENERATION_FAILED` 错误码。
5. 文档变更同步：资源模型变更 → openapi schema + 数据库表；`structure-contract.md` 的 `prompt_version` / `schema_version` / `rubric_version` 必须与 `agent_evolution/manifest.json` 一致。

## 工具链

- Python 环境统一使用 Conda 环境 `shanka-backend`（Python 3.12）；交互会话先 `conda activate shanka-backend`，Agent/脚本优先使用 `conda run -n shanka-backend ...`，禁止把项目依赖安装到 base 或系统 Python。
- Conda 只负责解释器与环境隔离；Python 依赖及 lint 配置仍以 `main/pyproject.toml` 为唯一事实源，不另建重复依赖清单。
- 依赖与 lint 配置唯一事实源：`main/pyproject.toml`（ruff line-length 100、mypy strict）。
- 测试：`cd main && conda run -n shanka-backend python -m pytest`。各层职责见 `main/tests/*/AGENTS.md`；命名规范 `test_<模块>_<行为>`。
- pre-commit：ruff-format → ruff → mypy（`main/.pre-commit-config.yaml`）。
- 配置：pydantic-settings 单层配置类，默认值进代码，密钥/令牌走环境变量，禁止散落硬编码。
- 本机实施/验收从仓库根目录、权限为 `600` 且被 Git 忽略的 `.env` 加载 `DEEPSEEK_API_KEY`，并作为运行时输入走正式 Key/API 流程；禁止提交 `.env`，或把明文凭据写入 Conda env config、plan、fixture、命令参数、日志与测试报告。

## 约定

- 文档命名：小写 + 连字符，版本与 PRD 对齐（v2.1）；破坏性变更（删字段、改语义）须同步 PRD 与验收标准，兼容性变更只更新契约。
- `CLAUDE.md` 是 `AGENTS.md` 的符号链接：只维护 AGENTS.md；新增子目录 AGENTS.md 后执行 `ln -sf AGENTS.md CLAUDE.md`。
