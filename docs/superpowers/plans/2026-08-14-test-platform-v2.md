# test-platform v2（账号化）P7 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **结果（2026-08-14）**：3 任务全部完成，commits `fae1485`（P7-1）/ `1819e08` + `594d4c0`（P7-2）/ P7-3（成本闸门 + 收尾）；test-platform `python -m pytest tests/` 77 passed、`unittest discover` 6/6 复跑稳定；local quick/full 后端联调与真实 LLM 未运行（本机后端未启动、live 未获成本确认）——证据详见 docs/Progress.md ACC-P7 与 .superpowers/sdd/2026-08-14-test-platform-v2/task-3-report.md。

**Goal:** test-platform 完成账号化切换：client 支持 register/login/set_token/logout 与敏感路径脱敏，runner 删除 --device-id，凭据只从环境变量读取，auth/isolation/核心业务/generation/observability 最小场景落地（无 legacy 场景），成本闸门改为运行前最坏预算推导 + 运行后 ledger 对账。

**Architecture:** 3 任务：client 账号化 → 场景改造 → 成本闸门与验收。test-platform/ 为顶层目录（纯 stdlib 黑盒 HTTP，与外层 main/ 同仓库）。

**Tech Stack:** Python 3.12 纯 stdlib（urllib/json），pytest（test-platform/tests 既有模式）。

## 全局约束

1. 工作目录 `/home/kbzz1/shanka_backend/test-platform`；解释器 `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`；自测 `python -m pytest tests/`（test-platform 目录内）。
2. 纯 stdlib 与黑盒 HTTP 原则不变（WORKER_PROMPT §6：保持纯 stdlib 与黑盒 HTTP）。
3. 凭据只从 `SHANKA_TEST_USERNAME` / `SHANKA_TEST_PASSWORD` 读取；不得出现在 CLI、console、JSONL；prod 必须显式确认且禁止自动注册/legacy claim。
4. 无 legacy 场景；quick/full 默认不调用真实 LLM；live 用受控小夹具。
5. 敏感路径脱敏（服务端与 client 统一）：Authorization/密码/token 不进日志/console/JSONL。
6. 不碰 docs/llm-account-long-run-v1/、docs/account-auth-test-platform-long-run-v1/（只读）；不部署不 push；live 运行需成本/Key 确认，未运行明确写未运行。

## 现状基线（2026-08-14）

- `shanka/client.py`（DeviceClient 或类似，X-Device-ID 注入）、`shanka/environments.py`、`shanka/cost.py`、`shanka/logging.py`、`shanka/cleanup.py`、`shanka/report.py`、`runner/suites.py`（含 --device-id 参数，此前 ruff 扫描见过）、`scenarios/baseline/api_smoke.py`（161 行）、`scenarios/flow/live_flow.py`（211 行）、`tests/`（5 个测试文件）。

## 任务

### Task 1: client 账号化 + runner 改造

**Files:**
- Modify: `shanka/client.py`、`runner/suites.py`、`shanka/environments.py`（凭据 env 读取）
- Test: `test-platform/tests/test_client.py`（更新/新增）、`test_environments.py`

**接口与行为**：
- client 新增：`register(username, password)` / `login(username, password)` / `set_token(token)` / `logout()`；普通请求 `Authorization: Bearer <token>`（token 由 set_token 持有）；register/login 不带头；彻底移除 X-Device-ID 注入。
- runner 删除 `--device-id` 参数；凭据 `SHANKA_TEST_USERNAME`/`SHANKA_TEST_PASSWORD` 读取（缺失 → 报错退出码非 0，不自动注册）；prod 目标环境显式确认（如 `--prod` 需交互确认或环境变量开关）且禁止自动注册。
- 敏感脱敏：client 与 logging 统一——Authorization/密码/token 不进 console/JSONL（脱敏形状 `Bearer ***`、密码不打印）。

- [x] **Step 1: 写失败测试**（test_client：set_token 后普通请求带 Bearer 无 X-Device-ID、register/login 不带头、logout 语义；test_environments：凭据 env 缺失报错）
- [x] **Step 2-4: 红→实现→绿**（TDD；`python -m pytest tests/` 全绿）
- [x] **Step 5: Commit**（外层仓库）：`feat(test-platform): P7-1 client 账号化——Bearer/四端点/脱敏 + runner 删 --device-id + 凭据 env`

### Task 2: 场景改造（auth/isolation/核心/generation/observability，无 legacy）

**Files:**
- Create: `scenarios/auth/`（auth 场景）、修改 `scenarios/baseline/api_smoke.py`、`scenarios/flow/live_flow.py`
- Test: 场景单元测试（无网络 mock 逻辑层）

**行为**：
- auth 场景：register/login/logout/me 最小链路（凭据来自 env；prod 禁自动注册）；isolation 场景：两用户资源 404/隔离；cards-review-stats 场景：既有 api_smoke 改造为 Bearer 流程；pdf/generation 场景沿用（Bearer）；observability 场景：quality-summary 按 user。
- 全部场景删除 X-Device-ID 与 legacy 语义；无行为占位文件。
- 场景结束清理业务资源与 session；无法安全删除的 local 测试 user 行按 run_id 计数报告，不新增生产账号删除接口。

- [x] **Step 1-4: TDD 循环**（逻辑层测试 + `python -m pytest tests/` 全绿）
- [x] **Step 5: Commit**：`feat(test-platform): P7-2 场景账号化——auth/isolation/核心/generation/observability（无 legacy）`

### Task 3: 成本闸门改造 + local 验收 + 收尾

**Files:**
- Modify: `shanka/cost.py`、`runner/suites.py`（闸门逻辑）、`scenarios/flow/live_flow.py`（对账）

**行为**：
- 删除「live 固定 3 次调用」假设：运行前用 fixture 与配置推导最坏调用预算（Planner/Generator/Scoring 调用数与 token 上限推导）；运行后以该用户 `llm_call_attempts` 对账实际 attempts/token/成本。
- 真实调用需单独成本确认（预算上限内才跑）；未运行明确写未运行。
- local quick/full 验收：真实命令跑 `runner quick`/`runner full`（不调真实 LLM 的路径——若无 local 后端则用受控最小路径验证 CLI 形状与闸门，报告如实记录运行范围）。
- 收尾：STATUS.md P7_DONE + Progress.md ACC-P7 + TASKS.md P7 勾选。

- [x] **Step 1-4: TDD 循环**（cost 推导/对账逻辑层测试）
- [x] **Step 5: Commit**：`docs(test-platform): P7 完成——成本闸门对账 + local 验收 + 收尾`
