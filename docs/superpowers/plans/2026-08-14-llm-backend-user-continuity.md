# LLM 后台 user_id 接续 P5 实施计划

> **For agentic workers:** 本阶段为验证型收尾（判别测试为主，少量断言补强），由主 Worker
> 直接执行（P2 先例：验证型阶段不派 subagent）；改动范围小、可逐项验证。

**Goal:** 锁定 P4 已接线的 LLM 后台 user_id 归属语义——tasks.user_id 为后台执行身份、后台不依赖 session/token、logout 后任务继续、账本/观测/成本按 user_id、资产只读——全部以真实判别测试固化，不改变任何 LLM 语义。

**Architecture:** P4 已把 executor Key 查找（按 task.user_id）、ledger（user_id 写入）、observability/quality-summary（user_id 聚合）切换完毕；P5 不新增接线，只补判别测试锁定 DESIGN §6 与 WORKER_PROMPT 目标二 §4 的冻结语义，并核对 v3/v2 资产只读性。

**Tech Stack:** pytest + 既有测试基建（conftest auth_headers、mock transport、fake LLM）。

## 全局约束

1. `cd /home/kbzz1/shanka_backend/main`；解释器 `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`。
2. 四工具全绿：`python -m pytest` 全部、`ruff check .`、`ruff format --check .`、`mypy .`。
3. **不改 LLM 语义**：v3/v2 资产（prompts/schemas/rubrics）、manifest 版本、状态机、配额、CAS、fingerprint 推导一律不动——本计划只加测试与注释，不改生产逻辑（若测试暴露缺陷则登记，不擅自改语义）。
4. 后台无 bearer/token：executor 链路不得读 Authorization/principal/session——判别测试锁定。
5. 敏感信息不进日志/测试报告/命令参数；测试用 mock transport（不触真实 DeepSeek）；不碰两个 long-run 目录；git add 只限本计划文件清单。

## 现状基线（2026-08-14，P4 完成 c28a0e0）

- executor `_decrypt_api_key` 按 task.user_id 查 Key（services/tasks/executor.py:86-90）；ledger user_id 写入（services/generation/ledger.py:50/74）；operation_key=`planning:{chapter_id}:{gi}` 纯任务域；metrics.py 无身份引用；services/tasks+generation 无 logout/revoked/session 引用。
- 已有覆盖：E2E 判别测试（test_task_e2e_user_domain.py：COMPLETED 6 卡 user_id 非空）；账本 user_id 断言部分在 T3 测试。

## 任务

### Task 1: 后台身份与 session 独立性判别测试

**Files:**
- Create: `main/tests/integration/test_background_user_continuity.py`

**测试用例（全部先写后验证；预期当前已绿——若红则暴露缺陷，登记后由控制器裁决，不擅自改语义）**：

1. `test_task_continues_after_logout_and_new_session_reads`：register 用户 → PUT key（mock）→ 上传 PDF → create task → logout（204）→ executor scan_once 跑完 → 任务 COMPLETED（logout 不中断后台）；login 新 session → GET /tasks/{id} 200 可读、卡片可读。
2. `test_task_continues_after_session_expiry`：创建任务后把 auth_sessions.expires_at 直接 UPDATE 为过去（模拟过期）→ executor 跑完 → COMPLETED；新登录可读（后台不因原 session 过期中断）。
3. `test_executor_path_has_no_session_dependency`：代码级判别——grep 语义以测试固化：`services/tasks/executor.py` 与 `services/generation/` 源码不含 `Authorization`/`bearer`/`principal`/`request.state`（读文件断言——防未来回归重新引入 session 依赖）。
4. `test_operation_key_and_cas_are_task_domain_only`：operation_key 格式 `planning:{chapter_id}:{gi}`（现 E2E 已隐式覆盖；本测试读 ledger 行断言 operation_key 不含 user_id/session_id 前缀）；同任务两轮 scan 不重复生成（CAS/账本幂等不依赖 session——现有测试兜底，此处直调 executor 两次断言账本行数守恒）。
5. `test_cross_user_ledger_and_task_404`：user2 的 GET /tasks/{user1_task_id} 404、/observability 只见自己数据（quality-summary user2 查询不含 user1 批次）。
6. `test_metrics_endpoint_has_no_identity`：/metrics 响应文本不含 user_id/username/session_id 字样（无身份聚合）。

**验收**：全量 pytest 绿（557+新增）；四工具全绿。

**提交信息**：`test(account-auth): P5-1 后台 user_id 接续判别——logout/过期后任务继续 + session 零依赖 + 跨用户 404 + metrics 无身份`

### Task 2: 账本/观测 user_id 断言补强 + 资产只读核对 + 收尾

**Files:**
- Modify: `main/tests/integration/test_task_e2e_user_domain.py`（或既有账本测试）——补账本行 user_id 断言（每 llm_call_attempts 行 user_id == task.user_id、device_id IS NULL）
- Modify: 无生产代码（只测试）；如 observability 测试已有 user_id 断言则确认覆盖

**核对项（真实命令，证据进报告不落 repo）**：
1. `grep -rn "Authorization\|bearer\|principal" main/services/tasks main/services/generation` 仅注释/无命中——后台零 session 依赖。
2. v3/v2 资产只读：`git log --oneline main/agent_evolution` P4/P5 期间无资产提交（P4 未触碰）；manifest 守卫测试绿（已含）。
3. 账本 user_id 断言落库后全量回归。

**验收**：四工具全绿；`docs/Progress.md` ACC-P5 条目 + `docs/llm-account-long-run-v1/STATUS.md` P5_DONE + `TASKS.md` P5 勾选。

**提交信息**：`docs(account-auth): P5 后台 user_id 接续完成——账本断言补强 + 资产只读核对 + 收尾`
