# R1 本机契约回归、受控真实模型验证与交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 跨闭环收敛：本机契约回归（AC-01～11 后端职责全过 + 干净环境安装/启动/迁移/重启恢复）→ LOCAL 门槛全过后才开放受控 DeepSeek live 验证（60 个生成单元、canary、¥5/¥10 硬上限）→ 统计报告 + 人工复核 + Progress R1 DONE + 最终交付报告。

**Architecture:** 两阶段硬门槛（本机 → live）。本机回归 = 既有 348 测试 + TestClient 路径核对清单 + 干净环境复现；live = 真实 .env Key 走正式应用链路（PUT /api-key → 任务 → executor → DeepSeekClient 真实调用），显式 scan_once 驱动（AC-05 模式），成本监控器实时累计（cost.py 价格常量），canary/上限提前停止。live 驱动脚本可提交（无 Key），Key 仅运行时从根目录 .env 读取。

**Tech Stack:** FastAPI TestClient + Alembic + uvicorn + pypdf（真实 PDF 文本层抽取）+ httpx（adapter）+ cost.py 成本估算 + scipy/statistics（Clopper-Pearson 精确区间）。

## Global Constraints

- **安全红线（最高优先级）**：真实 Key 仅在 live 执行时从 `/home/kbzz1/shanka_backend/.env`（600，gitignored）加载，走正式 Key/API 流程；禁止写入 plan/fixture/脚本/命令参数/日志/测试报告；日志与报告只输出掩码（sk-****）。凭据 smoke 已完成（2026-08-10），不得重复。
- **LOCAL-DONE 前禁止真实 DeepSeek 调用**：T1/T2/T3 全部 mock/无触网；T4 仅在 T1-T3 全过后启动。
- **LIVE-CAPPED 边界**：冻结 `deepseek-v4-flash` + thinking disabled + JSON output（Settings 默认即此，R-09）；60 个 generation unit（第 1/2/6 章各 20 个分散文本块，easy/medium/hard = 24/24/12）；第一单元 = canary，失败即停；每次硬上限 ¥5、总上限 ¥10，达到立即停止并保留真实失败；正式样本默认只运行 1 次，仅实质修复后才允许完整重跑 1 次。
- **统计口径（R-05）**：60/60 成功 → 单侧 95% 失败率上界约 4.9%（Clopper-Pearson）；有失败 → 原始比例 + 精确区间；不得外推全书/生产质量。
- 环境代理：本机 HTTP_PROXY=127.0.0.1:7897——**adapter 必须 `trust_env=False`**（直连，不继承 shell 代理；uvicorn 冒烟 502 教训）；真实调用不走代理。
- 工程边界与既有红线全部沿用（app→services→infra、红线 4/5、错误码守卫、四工具全绿）。
- PDF 只做程序化文本层+书签解析（无 OCR/视觉/截图），不复制/提交 PDF 文件；抽样框预先固定（固定 seed）。
- 测试命名 `test_<模块>_<行为>`；conda env `shanka-backend`；分支 codex/r1。

---

### Task 1: 本机契约回归 + PRD 7-10 路径核对清单

**Files:**
- Create: `main/tests/acceptance/test_acceptance_r1_paths.py`（PRD 7-10 后端可本机验证项的路径核对用例——缺失项补齐）
- Test: `main/tests/contract/`、`main/tests/acceptance/`（全量回归）

**范围（本机门槛 1）**：
- 四工具全量（348 基线确认 + 新增）。
- contract/acceptance 全量 + 逐项核对 PRD 7-10 可本机验证项：
  - 7.1 数据安全：Key 不落日志（AC-08 已测）、资源隔离（跨设备 404，V1-V6 已测）、级联删除（V1 已测）、日志不记完整 PDF/Prompt（AC-08 已测）——核对清单打勾。
  - 7.2 可靠性：分批/断点续传（AC-05）、已完成批次不重复（AC-05）、已入库卡不重复（AC-04/05）、失败保留游标（AC-05）——已测，核对。
  - 8.1 技术指标（8.1 是 live 后统计口径，本机只核对观测设施存在）：/metrics 输出 llm_* 指标（V5A 已测）。
  - 8.2/8.3 观测：Rubric/Cache 记录（AC-07 已测）、batch 观测列（V5A 已测）——核对。
  - 9 AC-01～11：全部已映射（V1-V6 acceptance）；本任务逐条核对映射存在性（如缺则补用例）。
  - 10 质量策略：Rubric 只观测（AC-04 已测）——核对。
- 生产代码无 mock/硬编码成功路径：grep `mock|fake|MagicMock` 于 `main/app main/services main/infra`（排除 tests）→ 允许项仅：V4 样卡 fake（契约设计，任务执行已不用）与测试注入点；其余一律清除。

- [ ] **Step 1: 运行全量四工具确认基线**（`cd main && conda run -n shanka-backend python -m pytest -q` / ruff / format / mypy；Expected: 348 passed 全绿）
- [ ] **Step 2: 建立核对清单**（`main/tests/acceptance/test_acceptance_r1_paths.py` 新建，docstring 以表格列 PRD 7.1/7.2/8/9/10 逐项 ↔ 既有测试名 ↔ 本文件补充用例；缺失项补最小用例——先写失败测试）
- [ ] **Step 3: 实现补充用例**（若核对发现缺口；否则仅清单）
- [ ] **Step 4: 生产代码 mock 核对**（grep 上述目录；允许项记录在案，不允许项修复）
- [ ] **Step 5: 全量验证 + 提交**（`test(r1): PRD 7-10 路径核对清单 + 缺口用例（R1 本机门槛 1）`）

---

### Task 2: 干净环境复现（安装/启动/迁移/重启恢复）

**Files:**
- Create: `docs/superpowers/plans/` 下不建；验证脚本放 `/tmp/r1-clean/`（临时，不提交）
- 记录: `docs/Progress.md` R1 段（T6 一并）

**范围（本机门槛 2）**：
- 干净 venv（python3.12 -m venv /tmp/r1-clean/venv）→ 锁定安装（`pip install -r main/requirements-dev.lock`——R-02 唯一锁定方式）→ `alembic upgrade head` → 启动（直接解释器 uvicorn，避开 conda run 静默问题）→ healthz/readyz 200 → 迁移后 schema 核对（sqlite .tables 12+ 表）。
- 重启恢复：写入数据（建牌组/任务）→ 杀进程重启 → 数据保留 + 应用可用；任务 RUNNING 心跳恢复路径（V5B 孤儿恢复——应用级确认，已由测试覆盖，此处验证进程级）。
- 环境变量：DEEPSEEK_API_KEY 不注入（本任务不触网）；DATABASE_URL/STORAGE_PATH 指向 /tmp。

- [ ] **Step 1: 干净 venv + 锁定安装**（记录安装耗时与版本摘要）
- [ ] **Step 2: 迁移 + 启动 + 探针**（alembic upgrade + uvicorn + curl healthz/readyz）
- [ ] **Step 3: 写入 + 重启恢复**（建数据 → 重启 → 校验保留）
- [ ] **Step 4: 验证记录 + 提交**（本任务无代码提交——仅验证记录进 Progress（T6）；若发现缺口则修复并提交）

---

### Task 3: live 驱动设施（adapter 加固 + 抽样框 + driver）

**Files:**
- Modify: `main/infra/llm/deepseek.py`（`httpx.Client(..., trust_env=False)` + chat 返回加 `system_fingerprint` 透传）
- Modify: `main/tests/integration/test_deepseek_adapter.py`（或既有 adapter 测试）加两条断言（trust_env 不可直接断言——改为构造无代理依赖的说明性用例 + fingerprint 透传断言）
- Create: `main/tests/live/sample_frame.py`（60 文本块抽样框：读真实 PDF 第 1/2/6 章 → 固定 seed 分散取 20 块/章 → 输出 60 章节定义 JSON；程序化文本层+书签，无 OCR）
- Create: `main/tests/live/driver.py`（live 执行驱动：加载 .env（仅运行时）→ PUT /api-key → 逐单元正式链路 → 成本监控 → 停止条件 → 结果记录；无明文 Key 落盘）
- Create: `main/tests/live/README.md`（执行说明：前置条件/命令/输出/停止条件/Key 安全）

**Interfaces:**
- Consumes: `estimate_cost_by_kind`（cost.py）、`services.tasks.executor.scan_once`、`services.generation.schema_validator.validate_card`、TestClient（create_app）、`.env`（仅运行时）。
- Produces:
  - `sample_frame.py --out /tmp/r1-frame.json`：60 章节定义（chapter_name/start_page/end_page/file_id 占位）+ 固定 seed 记录。
  - `driver.py --frame /tmp/r1-frame.json --db <path> --storage <path> [--max-cost-yuan 5] [--max-total-yuan 10] [--dry-run]`：逐单元执行 + 输出 JSON 结果文件（每单元：task_id/model/fingerprint/tokens/价格/状态/耗时；汇总：成功/失败/总成本）。

**driver 单元流程（正式链路）**：
1. 上传真实 PDF（POST /pdfs，multipart）→ 解析（V3A 三重校验）→ 抽样块 → 每块创建 chapter 行（DB 直插，driver 内，非 HTTP——章节配置接口已由 AC-02 覆盖，live 关注生成链路）。
2. 建牌组（POST /decks）。
3. PUT /api-key（真实 Key，掩码输出；仅首次）。
4. 逐单元：POST /tasks（chapter_ids=[块章]、quantity_tendency 按难度映射 COMPACT/BALANCED/EXTENSIVE、幂等键 K）→ 显式 `scan_once`（settings 注入 task_scan_interval_seconds=3600 禁用自动）→ 任务 COMPLETED 验证 → 卡 Schema 合法（validate_card 直读 DB）→ 入库计数 = 计划数 → 幂等重放（同幂等键重发 POST /tasks → 同响应 + 不重复执行）→ 记录 usage/model/fingerprint/价格 → 成本累计检查（canary 后每单元检查，超 ¥5 或总 ¥10 → 停止保留真实失败）。
5. 输出报告 JSON + 掩码日志。

- [ ] **Step 1: adapter 加固 + 测试**（trust_env=False + system_fingerprint 透传；fingerprint 断言：mock 响应含 fingerprint → chat 返回含同值）
- [ ] **Step 2: sample_frame.py**（真实 PDF 程序化抽取 60 块；dry-run 输出 JSON 供审查——**抽样框在 live 前由主 Agent 审阅固定**）
- [ ] **Step 3: driver.py + README**（dry-run 模式全流程 mock 走通：注入 mock transport 验证单元流程/成本监控/停止条件逻辑）
- [ ] **Step 4: 全量验证 + 提交**（`feat(live): R1 live 驱动设施（adapter trust_env/fingerprint + 60 抽样框 + driver）`——dry-run 全绿、四工具全绿；真实 live 仅在 T4 执行）

---

### Task 4: LIVE-CAPPED 执行（主 Agent 亲自）

**Files:**
- Create: `docs/r1-live-report.md`（live 结果报告：每单元明细 + 汇总统计；无明文 Key；随分支提交为证据）

**执行流程（严格按 Global Constraints）**：
- [ ] **Step 1: 前置核验**：T1-T3 全绿；`.env` 存在（600，gitignored）不读内容；agent 无 key 知识——driver 运行时才读取。
- [ ] **Step 2: 抽样框固定**：运行 sample_frame（seed 固定）→ 主 Agent 审阅 60 块定义（章/页范围/文本非空）→ 冻结。
- [ ] **Step 3: canary**：driver 执行单元 1（canary）→ 成功继续同一次运行；失败 → 停止，记录真实失败，回 T3 修复后重跑（仅允许 1 次完整重跑）。
- [ ] **Step 4: 60 单元正式执行**：逐单元串行；每单元后成本检查（¥5 单次 / ¥10 总硬上限）；任何失败保留真实失败并记录。
- [ ] **Step 5: 结果固化**：`docs/r1-live-report.md`（model/fingerprint/tokens/价格/耗时/状态逐单元 + 汇总；统计按 R-05 口径）+ 日志掩码核对（无明文 Key）。
- [ ] **Step 6: 成本与边界核对**：实际花费 vs ¥5/¥10；fingerprint 记录；价格配置（cost.py 2026-08-11 档）当日价格。

---

### Task 5: 统计 + 人工复核 + Progress R1 DONE + 最终交付（主 Agent）

**Files:**
- Modify: `docs/Progress.md`（R1 DONE + 冲突登记更新）
- Create: `docs/r1-live-report.md`（T4 产物完善：统计区间 + 人工复核段）

- [ ] **Step 1: 统计**：60/60 → Clopper-Pearson 单侧 95% 失败率上界（0/60 → 约 4.9%，用 scipy.stats.beta 计算精确值）；有失败 → 原始比例 + 双侧精确区间；对照 8.1 指标（完成率/重复入库率——live 实证值）；不外推。
- [ ] **Step 2: 人工复核**：从产出卡按章节×难度分层抽 18 张（固定 seed）→ 描述性报告（正确性/清晰度/学习价值主观评价，非门槛）。
- [ ] **Step 3: Progress 更新**：R1 DONE（本机回归证据 + live 证据 + 统计 + 复核）；冲突登记（R-05 统计结论、R-06 部署边界、R-09 冻结记录、R-14 遗留、新登记：live 实际调用/成本）。
- [ ] **Step 4: 最终交付报告**：按 /goal 要求：工作包/分支清单、每包实际验收命令+结果、Progress 变化（F0-R1 全 DONE）、DeepSeek 实际调用/token/费用/边界、未验证外部范围（Tunnel/TLS/真机/OCR/多实例）、剩余冲突/风险/用户决策事项（R-03/05/06/09/14/17/18/19 + 新发现）。
- [ ] **Step 5: 合并 + main 复验 + 清理**（finishing-a-development-branch 流程；main 上组合验收 + worktree 清理）

---

## 自审记录（writing-plans skill）

**Spec 覆盖（Progress R1 段逐句对照）**：
- 「先运行 contract/acceptance」→ T1 ✓
- 「通过 TestClient/localhost 核对路径、错误、脱敏、文件/DB 恢复、慢查询和 PRD 8 可本机采样指标」→ T1（路径/错误/脱敏）+ T2（文件/DB 恢复）+ T1（指标）✓
- 「清除 Mock/硬编码成功路径」→ T1 Step 4 ✓
- 「干净本机环境完成锁定安装、启动、迁移和重启恢复」→ T2 ✓
- 「生产 DeepSeek adapter 必须完成；adapter 使用 mock HTTP transport 验证」→ 已完成（V3B/V5A）；T3 加固（trust_env/fingerprint）✓
- 「60 个 generation unit…canary…¥5/¥10…记录 model/fingerprint/token/版本/价格」→ T3/T4 ✓
- 「60/60 → 4.9% 上界；失败 → 原始比例与精确区间；不外推」→ T5 ✓
- 「人工复核 18 张分层抽样，描述性」→ T5 ✓
- 「正式样本默认只运行 1 次，实质修复才允许重跑 1 次」→ T4 ✓

**Placeholder 扫描**：无 TBD/TODO；dry-run 与 live 分离明确；driver 接口签名具体。

**类型一致性**：`sample_frame.py --out` 输出 60 章节定义 JSON ↔ `driver.py --frame` 消费；`driver.py` 的 --max-cost-yuan/--max-total-yuan 与 ¥5/¥10 常量一致；`estimate_cost_by_kind` 返回 dict 键名（cache_hit/cache_miss/output/total）在 driver 中消费一致。
