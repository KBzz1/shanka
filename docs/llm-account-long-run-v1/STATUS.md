# LLM 链路升级 + 账号登录与测试平台：合并长程任务执行状态

## Goal Lock

- Goal ID：`shanka-llm-account-long-run-v1`
- Goal：先完成 LLM 链路升级（17 任务，P1），再完成账号登录替代 X-Device-ID（PRD V2.2，P2–P8）。
- 当前状态：`GOAL_DONE`（P8 总验收完成：563/0 四工具全绿 + 迁移副本往返零漂移 + sentinel 扫描干净 + WORKER_REPORT.md 落盘；两目标全部完成，最终门禁 8 项全勾）
- 设计：`/home/kbzz1/shanka_backend/docs/llm-account-long-run-v1/DESIGN.md`（引用两份上游设计）
- 启动提示词：`/home/kbzz1/shanka_backend/docs/llm-account-long-run-v1/WORKER_PROMPT.md`
- 任务地图：`/home/kbzz1/shanka_backend/docs/llm-account-long-run-v1/TASKS.md`
- 最后更新：2026-08-14 Asia/Shanghai（GOAL_DONE）

## 现场快照（2026-08-13 接管时）

- 分支：`main`；HEAD：`8cd0cb5010316359e45003a985ddab2fe35c6188`（与设计期快照一致，无新提交）
- Alembic head：`ead86a96d103`（`main/migrations/versions/` 仅 initial + `0002`，无 LLM 0003）
- 未提交差量（17 修改 + 未跟踪资产）—— 属本包 P1 施工中产物，不是外部任务，不得覆盖：
  - `agent_evolution/`：`CHANGELOG.md`、`manifest.json`、`prompts/AGENTS.md`、`rubrics/AGENTS.md`、
    `schemas/AGENTS.md`；未跟踪 `prompts/v3/`、`rubrics/v2/`、`schemas/v2/`
  - `docs/Architecture/structure-contract.md`（8.5 节资产登记口径改写中）
  - `docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md`（+249 行差量，spec 仍在改）
  - `main/`：`infra/llm/deepseek.py`、`infra/llm/prompts.py`、`services/cards/rewrite.py`、
    `tests/acceptance/test_acceptance_ac04_ac07.py`、`tests/contract/test_manifest_guard.py`、
    `tests/integration/test_batches.py`、`tests/integration/test_cards_rewrite.py`、
    `tests/integration/test_observability.py`、`tests/unit/test_deepseek_adapter.py`、
    `tests/unit/test_prompts.py`；未跟踪 `tests/contract/test_prompt_assets_v3.py`
- 聚焦测试复核（2026-08-12/13 两次）：
  - 命令：`cd main && /home/kbzz1/miniconda3/bin/conda run -n shanka-backend python -m pytest -q tests/unit/test_prompts.py tests/unit/test_deepseek_adapter.py tests/contract/test_prompt_assets_v3.py tests/contract/test_manifest_guard.py tests/integration/test_cards_rewrite.py`
  - 结果：`45 passed, 1 warning`，退出码 0。
  - 结论边界：只证明当前落盘 v3/v2 资产与已接线增量的聚焦测试通过；不证明 LLM 升级完成。
- 原账号包 `docs/account-auth-test-platform-long-run-v1/` 状态为 `WAITING_FOR_LLM_BASELINE`；其
  "等待外部 worker 交接"的 Gate 0 已被本包 §2.2 改写为内部 P1 完成门禁，该目录保留为历史冻结设计。

## 范围变更记录

- 2026-08-13（用户决策）：**不做老数据迁移/认领**。合并任务包删除：legacy-claim/ticket 端点、
  claim ticket 机制、legacy 表复制、事件 ID 重映射、API Key 密文搬运、Android 认领提示与
  test-platform legacy 场景；owner 表改为加 user_id 列（新写入必填），旧 device_id 行原样保留
  （不迁不删、无访问路径），清理属后续单独发布。上游账号设计 §4.4/§5.3 的 legacy 部分不在本包范围。
- 已同步到 `WORKER_PROMPT.md`、`DESIGN.md`、`TASKS.md`；上游只读文件未改动。

## 恢复入口

- 最后已验证 checkpoint：P0 现场核验完成（2026-08-13）；实现开始 T1。
- 当前阶段：P1 LLM 升级实施（17 任务，subagent-driven，TDD 步骤以 LLM plan 为唯一权威）。
- P0 核验证据（2026-08-13，全部为真实命令）：
  - `git rev-parse HEAD` → `8cd0cb5010316359e45003a985ddab2fe35c6188`（与快照一致，无新提交）
  - `python -m alembic heads` → `ead86a96d103`（`migrations/versions/` 仅 initial + 0002，无 0003）
  - `git status --short --branch` → 17 修改 + 8 未跟踪项，与现场快照逐项一致（用户差量保护清单成立）
  - 聚焦测试：`cd main && /home/kbzz1/miniconda3/bin/conda run -n shanka-backend python -m pytest -q
    tests/unit/test_prompts.py tests/unit/test_deepseek_adapter.py tests/contract/test_prompt_assets_v3.py
    tests/contract/test_manifest_guard.py tests/integration/test_cards_rewrite.py`
    → `45 passed, 1 warning in 0.31s`，退出码 0
  - `.env` 存在且权限 `-rw-------`（600）✓（T17 canary 可用）
- 差量↔plan 一致性判定：无不可恢复漂移，P1 可继续。已施工 vs 缺失：
  - 已有施工（保留，不重做）：T5 资产文件（prompts/v3、rubrics/v2、schemas/v2）+ manifest 切换 +
    CHANGELOG + prompts.py `safe_json_dumps`（但 `asset_versions()` 8 键扩展与 `load_schema_asset`
    缺失 → T5 未完）；T6 部分（deepseek.py chat 已支持 system_prompt+max_tokens，缺
    `RetryableUpstreamError` → T6 未完）；rewrite 安全 JSON 信封（spec §5.5）；相关测试版本断言
    （test_prompts/test_deepseek_adapter/test_manifest_guard/test_prompt_assets_v3/test_batches/
    test_observability/test_cards_rewrite/test_acceptance_ac04_ac07）；structure-contract 8.5 节。
  - 缺失（按 plan 补齐）：T1–T4、T7–T13、T14（除 8.5）、T15–T17 无施工。
- 若 P0 发现差量不可恢复漂移：只在本文件追加证据，回传 `BLOCKED_ON_LLM_WORKTREE`。
- P1 完成（门禁见 DESIGN.md §2.2）后记录 `LLM_BASELINE_COMMIT` 与真实 Alembic head，进入 P2。

## P1 完成记录（2026-08-13，全部为真实命令/账本证据）

- **`LLM_BASELINE_COMMIT` = `a874944`**（fix(llm-upgrade): final review 收尾——rewrite 账本/双消息/max_tokens Settings 化）；P1 提交范围 main 分支 `8cd0cb5..a874944`（24 commits）+ 嵌套 frontend-app 仓库 `2a9f6b7`。
- **Alembic head = `2a391e994f93`**（0003 llm_pipeline_upgrade，空库 upgrade→downgrade→upgrade 往返 + `alembic check` 零漂移，T1 实测）。
- **17 任务全部完成且 review-clean**（SDD：每任务 implementer + spec/质量 reviewer 双审；T9/T11/T12/T14 各 1 轮 fix、全部 scoped re-review clean；最终整支审查 3 个 Important 经收尾修复 a874944 闭环）。
- **本地实现证据（LOCAL_IMPLEMENTATION_DONE）**：全量 **500 passed / 0 failed**；`ruff check .`、`ruff format --check .`（247 files）、`mypy .`（196 source files）四工具全绿（主 Worker 2026-08-13 实测）；契约守卫 44/44。
- **受控真实 canary（PRODUCTION_VALIDATED）**：真实 DeepSeek 单任务 Planner→Generator→Scoring 全链路**连续 3 次全 PASS**（每任务账本 7 行全 SUCCESS、评分 3/3 回写、成本 ≈¥0.015~0.029/次，全战役 ≈¥0.06 ≤ ¥3 上限；用户授权付费调用）；canary 暴露 2 个 mock 掩盖缺陷并修复（adapter 内部 HTTP 重试 ×1；thinking 显式禁用——上游默认启用 reasoning 挤掉 content 空响应根因），各带判别测试与全量回归。
- **R-03 已 RESOLVED**（docs/Progress.md 第 6 节 + LLM-P1 工作包条目 DONE）；spec 状态行完成说明（用户裁决：按 plan 更新）。
- 遗留登记（不阻塞 P2）：PRD 5.4.2 行 231/238 残留（spec §12 范围外，V2.2 收敛）；Settings.batch_size 死配置、build_generation_prompt 死代码、tasks/CLAUDE.md 陈旧表述（清理项）；REWRITE 孤儿 STARTED 无恢复路径（观测噪声，无预算影响）。

## P2 完成记录（2026-08-13，全部为真实命令/证据）

- **契约 V2.2 落盘**：`docs/PRD/V2.2/prd_v2_2.md`（继承 v2.1，决策 D-05 账号会话取代 D-02、D-06 旧数据不迁移不认领；FR-19/AC-12；无 legacy claim 端点、无 LEGACY_CLAIM_UNAVAILABLE）；`structure-contract.md` v2.2（1.1 Bearer+user_id、1.3 幂等域 (user_id,path,key)、1.4/1.6 WWW-Authenticate 口径与限流 user 维度、3.14/3.15、6.11 四端点、7 账号错误码组、8 日志 user_id、9 对照表）；`openapi.yaml` 2.2.0（BearerAuth + /auth/* 四路径 + 4 个 auth schema）；`database-design.md` v2.2（§0/§1 隔离键切 user_id；§7.1 重写为 V2.2 目标态：users/auth_sessions、owner 表 user_id 列、复合键、Alembic fail-closed 策略；§2 保持 v2.1 实现态与 ORM 守卫一致，随 P3 迁移同批更新）；`docs/frontend/` 两文档 Bearer 化；`main/app/errors.py` 4 个账号错误码 + localization keys（设备码保留至 middleware 退出）。
- **验证（主 Worker 2026-08-13 实测）**：`python -m pytest` **500 passed / 0 failed**（契约守卫 44/44）；`ruff check .` 全过；`ruff format --check .` 247 files；`mypy .` Success（196 source files）。修复 1 个 P2 自身缺陷：openapi Unauthorized 描述含 `WWW-Authenticate: Bearer`（冒号空格）被 YAML 判为映射分隔符 → 折叠标量修复。
- **边界**：P2 只落契约与错误码注册，不含 auth 端点/中间件/ORM/迁移实现；最终门禁「V2.2 正式契约与实现一致」待 P8。

## P3 完成记录（2026-08-14，全部为真实命令/账本证据）

- **Alembic 链**：`2a391e994f93 → ddc6f34e30b8 → a7cc699f3fd8`（真实 heads 派生，不硬编码 0004）；空库 `upgrade head`（5 revisions 全链）+ `alembic check` 零漂移（主 Worker 实测，exit 0）。
- **提交范围**：main 分支 `3464e9c..89ce41d`（5 commits：90e01e3 数据地基 / 503dea7 T1 review 修复 / d07f316 主键重建+fail-closed / 5be727a+89ce41d final review 注释修复）。
- **schema 落地**（database-design §2 与 ORM 同批翻新，守卫强制一致）：users/auth_sessions 新表；8 个 owner 表 user_id 列 + device_id 降级遗留 NULL + CHECK 双非空；api_keys PK→user_id、idempotency_keys PK→(user_id, path, idempotency_key) + 保留遗留 UNIQUE(device_id, path, idempotency_key)；review_events 另加 UNIQUE(user_id, client_event_id)。
- **旧数据保留（D-06）**：9 表 SQL 直插旧库副本 upgrade 行数守恒（自动化判别测试）+ final reviewer 双层 downgrade 实测无丢失；PDF storage manifest 零差量；无密文搬运。
- **downgrade fail-closed**：users 非空或任一 owner 表 user_id 非空 → 任何 DDL/DML 前拒绝；空库/纯旧副本正常降级。
- **env.py 连接层关闭 FK 强制**（防 batch 重建级联清空，reviewer 核实必要且正确）+ SQLAlchemy NULL 主键三重限制的 ApiKey mapper 改写 / api_key 覆盖路径 Core 化（reviewer 裁决为 v2.1 行为不回归的必要等价手段）。
- **验证（主 Worker 2026-08-14 实测）**：`python -m pytest` **508 passed / 0 failed**；`ruff check .` 全过；`ruff format --check .` 249 files；`mypy .` Success（198 source files）。
- **SDD 审查**：2 任务 × implementer + 任务级 reviewer 双审；整支 final review（fable）With fixes → 2 fix rounds 闭环 → clean。
- **P4 跟进清单**：见 docs/Progress.md ACC-P3 条目（① api_keys UNIQUE(device_id) 决策、② ddc6 层 CI 断言、③ §7.1 措辞、④ 写侧债务三条）。
- **边界**：P3 只落数据层（ORM/迁移/测试），不改 services/app 业务逻辑（唯一例外：api_key 覆盖路径 Core 化——语义逐字等价，v2.1 按 device_id 行为不回归由全量回归背书）。

## P4 完成记录（2026-08-14，全部为真实命令/账本证据）

- **提交范围**：main 分支 `1e52f96..8087245`（13 commits：T1 9decae6+1283a6a / T2 0b88827 / T3 9c1c1ff+b53461c / T4 e87190e+9577904 / T5 da7ca8e / T6a d0c2c36+4888d6b / T6b cfa77f4 契约收尾 / 8087245 final review Minor）。
- **账号体系落地**：services/auth（Argon2id 19456/2/1 参数守卫 + dummy 校验 + 256-bit opaque token SHA-256 摘要）、/auth 四端点（register 201/login 200/logout 204/me 200；INVALID_CREDENTIALS 401 无 WWW-Authenticate、USERNAME_TAKEN 409）、BearerAuthMiddleware（AuthPrincipal 注入；401 AUTH_REQUIRED/AUTH_INVALID 带 WWW-Authenticate: Bearer；error_handler 统一加头覆盖窄竞态）。
- **X-Device-ID 退出**：DeviceIDMiddleware 删除；运行时代码 X-Device-ID 与 state.device_id 读取归零（grep 佐证）；devices 表不再自动创建/刷新（仅兼容审计）；errors.py 设备两码移除；structure-contract ch7 设备组移除。
- **全链路 user_id 归属**：9 handlers + 14 services 全部切 principal.user_id；新写入不再生成 device_id；幂等域 (user_id, path, key)；限流业务维度 user_id + auth 维度（register/login IP 20/h + login 用户名 10/h）+ IP 总闸门独立中间件（ip_limit.py，未认证流量覆盖）；跨用户统一 404 一致性守卫（Card↔Deck/Task↔PDF/Deck/LlmCallAttempt↔Task）；api_keys PK→user_id 用户域 Core 直写（mapper 移除回 ORM）；UNIQUE(device_id) 迁移（e85c78b2a345，P4 跟进 a）；ddc6 层 downgrade 带旧行 CI 断言（跟进 b）；§7.1 fail-closed 生效范围与归属措辞（跟进 c）；P3 写侧债务三条全部闭环（跟进 d）。
- **契约收尾**：openapi /auth/me 200 补 user 包装层（P2 遗留漂移修复）+ AuthRegisterRequest/AuthLoginRequest minLength/maxLength；structure-contract §1.3/§6.11 logout 顺序重放语义句 + login 400 澄清句 + logout 幂等列；database-design 2.1 devices 仅兼容审计。
- **敏感脱敏**：日志身份字段 user_id（app+infra 双处）；敏感脱敏判别测试（Authorization/密码 sentinel 不进日志）；scanner 解析失败日志 device_id 字段移除。
- **验证（主 Worker 2026-08-14 实测）**：`python -m pytest` **557 passed / 0 failed**（含契约守卫全等）；`ruff check .` 全过；`ruff format --check .` 271 files；`mypy .` Success（218 source files）；空库 `alembic upgrade head`（6 revisions 全链）+ `alembic check` 零漂移 exit 0。
- **SDD 过程**：6 任务 × implementer + 任务级 reviewer；T4 边界调整裁决（Key 写侧提前、T5 收缩）；fix rounds 6 轮全部 scoped re-review clean。
- **遗留登记（不阻塞）**：driver report["device_id"] 字段（reviewer 判定不违反 §4.5，P7 test-platform 裁决）；RateLimitMiddleware write 桶 60s 窗口 clock 注入留后续（ip 桶已注入）；tests/live driver dry-run mock 按请求区分 planner/generator 形状（P7 平台裁决）。

## P5 完成记录（2026-08-14，全部为真实命令/证据）

- **提交**：main 分支 commit 8a54b96（`tests/integration/test_background_user_continuity.py` 新建 230 行，6 个判别测试）。
- **判别锁定（DESIGN §6 / WORKER_PROMPT 目标二 §4 冻结语义，全部真实链路非 mock 断言）**：
  - logout 后任务继续：register→Key→PDF→牌组→任务→logout(204)→executor 扫描→COMPLETED 6 卡→新登录可读；
  - session 过期后任务继续：DB 回拨 expires_at→401→executor 仍跑完→新登录可读；
  - 代码级判别：services/tasks + services/generation 源码零 Authorization/Bearer/principal/request.state/auth_sessions 命中（防未来回归）；
  - operation_key 纯任务域（planning:/generating:/scoring: 前缀，不含 user/session 维度）+ 账本行 user_id==task.user_id、device_id NULL + 重复扫描账本行数守恒（CAS 不依赖 session）；
  - 跨用户：GET /tasks/{他人任务} 404、quality-summary 不含他人 task_id；
  - /metrics 无身份聚合（不含 user_id/username/session_id 字样）。
- **资产只读核对**：P4/P5 期间（1e52f96..HEAD）agent_evolution 零提交（git log 实测）；manifest 守卫随全量测试绿。
- **验证（主 Worker 2026-08-14 实测）**：全量 **563 passed / 0 failed**（557 + 6 新增）；`ruff check .` 全过；`ruff format --check .` 272 files；`mypy .` Success（219 source files）。
- **执行方式**：主 Worker 直接执行（验证型阶段，P2 先例）；P4 已把归属接线完成（executor Key 查找/账本/观测 user_id），P5 零生产逻辑改动。

## P6 完成记录（2026-08-14，全部为真实命令/证据）

- **提交**：嵌套 frontend-app 仓库 4 commits `60d62a3..f15457b`（外层仓库零提交——嵌套仓库纪律，P1 先例）。
- **落地内容**：KeystoreSessionStore（AES/GCM + Android Keystore 别名 shanka_session_key，token 加密存储、密码零持久化）替代 SecureDeviceIdentityStore；BackendClient Bearer 注入 + register/login/logout/me 四端点 + 401 语义（AUTH_REQUIRED/AUTH_INVALID 清会话、INVALID_CREDENTIALS 不清、网络失败不误判退出）；Login/Register Compose UI（密码掩码、错误文案映射、无 legacy claim）；启动路由（session + /auth/me 决定主界面或登录页）；普通请求彻底移除 X-Device-ID。
- **X-Device-ID 退出**：`X-Device-ID|SecureDeviceIdentityStore|shanka_device_identity` 全仓 grep 零命中（reviewer 独立复核）；androidTest 6 处断言 Bearer 化 + debug 脱敏守护换 Authorization。
- **预存在缺陷修复**（review 三路验证，阻塞全绿验收）：ImportParser fallback 分支重复报错（errors.isEmpty() 守卫 + 判别测试）。
- **验证**：`./gradlew test` **40/40 全绿**（AuthClientContractTest 15 + AuthViewModelTest 15 + SessionStoreContractTest 5 + ImportParserTest 3 + ReviewSchedulerTest 2）；`assembleDebug` + `assembleDebugAndroidTest` + `compileDebugAndroidTestKotlin` 全 BUILD SUCCESSFUL（reviewer --rerun-tasks 强制复跑非缓存）。
- **SDD 过程**：4 任务 × implementer + 任务级 reviewer（同一 reviewer 全程连续审查）；1 轮 fix round（ImportParser 预存在缺陷）。
- **未运行项**：instrumented 设备测试（本机无模拟器/真机，仅编译+打包验证——WORKER_PROMPT 验证 6 允许；BackendClientInstrumentedTest/FlashcardsAppTest 语义已更新待设备验证）。
- **遗留登记（不阻塞）**：FlashcardsAppTest.storedSessionEntersTheMainScreen 非 hermetic（后端在线环境会 401 失败——建议后续 AppViewModel 注入缝）；logout 先网络后本地（Settings 接入时改先本地登出）；logout 无 UI 调用方（Settings 留后续）。

## P7 完成记录（2026-08-14，全部为真实命令/证据）

- **提交**：外层仓库 test-platform/ 顶层目录（纯 stdlib 黑盒 HTTP，零外部依赖）3 任务 commits：`fae1485`（P7-1 client 账号化 + runner 删 --device-id）/ `1819e08` + `594d4c0`（P7-2 场景改造 + review fix）/ P7-3 成本闸门与收尾（本记录随该提交落盘）。
- **client 账号化（DESIGN 8.1）**：register/login/set_token/logout 四端点；普通请求 set_token 后自动 Bearer（未设置不带头）；register/login 恒不带头（auth=False 显式剥离，不依赖后端豁免）/不重试/不落事件；logout 带 Bearer + 幂等键且无论结果清空本地 token；X-Device-ID 注入彻底移除；PUT /api-key 与 auth 凭据路径统一脱敏不落日志。
- **runner 与凭据**：删除 `--device-id`；凭据只从 `SHANKA_TEST_USERNAME`/`SHANKA_TEST_PASSWORD` 读取（缺失拒绝 exit 1，不自动注册）；prod 必须 `--confirm-prod` 且只 login（禁自动注册）；run_id 由 runner 生成注入场景。
- **场景**（无 legacy、无占位文件）：auth（401 语义/me/logout 撤销）、isolation（两用户跨用户统一 404 + quality-summary 按 user + 异常路径前缀兜底清理）、api_smoke（Bearer 化 + 同键幂等重放带 Bearer）、live_flow（API Key→PDF→samples→任务→轮询→复习→看板→summary 端到端 + 观测临时账号交叉断言）。
- **成本闸门（DESIGN 8.3，废弃「live 固定 3 次调用」假设）**：运行前 `cost.derive_budget` 按受控 fixture（2 章 BALANCED）+ 契约默认上限镜像推导最坏预算 53 次调用（PLANNING 3 + GENERATING 36 + SCORING 12 + 固定 2；最坏输出 82944 token、最坏输入 600000 token、最坏成本 ≈¥1.86）> 阈值 3 → 必须 `--confirm-cost`（拒绝消息含逐阶段明细）；运行后经 `GET /tasks/{id}/batches` 对账实际批数/生成尝试/token/成本（报告字段 llm_budget_calls/llm_attempts_actual/llm_tokens_actual/llm_cost_actual）。**对账边界如实声明：后端无 llm_call_attempts GET 端点，PLANNING/SCORING 尝试数无 HTTP 观测入口，仅 GENERATING 阶段（批=单元账本投影）可对账。**
- **环境性修复（T2 review 发现）**：test_client 测试服务器地址改 localhost——HTTP_PROXY=127.0.0.1:7897 环境下 urllib `proxy_bypass('127.0.0.1')==False`（NO_PROXY 的 127.* 不匹配 IP 字面量）导致间歇走代理 502；localhost 实测 bypass==True，unittest discover 6 次复跑 6/6 稳定（修复前 6 次 3 失败）。
- **验证（主 Worker 2026-08-14 实测）**：test-platform `python -m pytest tests/` **77 passed / 0 failed**（T1 基线 29 → T2 64 → T3 77）；`python -m unittest discover -s tests` **77 tests × 6 次复跑全 OK**；CLI 形状实测：quick/full 无凭据拒绝 exit 1、live 无 `--confirm-cost` 拒绝且消息含预算明细、`--confirm-cost` 放行并打印确认行。
- **未运行项（如实声明，WORKER_PROMPT 纪律）**：local quick/full/live 对真实后端联调**未运行**（本机后端未启动，localhost:8000 无响应）——仅以受控最小路径（`--scenario auth`/`--scenario live_flow` 对 down 后端）验证 CLI 形状、闸门与失败记账；**真实 LLM 调用未运行**（live 需成本/Key 单独确认，本任务未获确认）。
- **SDD 过程**：3 任务 × implementer + 任务级 reviewer；T2 review Medium 1 项（register/login 带头）fix round 1/5 闭环；T3 继承 T2 3 项 minor（isolation 异常路径残留、auth --run-id help 文案、报告计数口径）+ 1 项环境性 flaky 全部闭环。

## 阶段账本

| 阶段 | 状态 | 证据/下一步 |
| --- | --- | --- |
| P0 现场基线核验 | `DONE` | HEAD 8cd0cb5；Alembic head ead86a96d103；聚焦 45 passed；差量一致无漂移 |
| P1 LLM 升级 17 任务 | `DONE` | `LLM_BASELINE_COMMIT`=a874944；Alembic 2a391e994f93；500/0 四工具全绿；canary 3/3；R-03 RESOLVED（见上 P1 完成记录） |
| P2 契约 V2.2 | `DONE` | 见上 P2 完成记录：PRD V2.2 + 四契约文档原子同步 + 500/0 四工具全绿 |
| P3 数据地基 | `DONE` | 见上 P3 完成记录：5 commits 3464e9c..89ce41d、Alembic 链 3 revisions、508/0 四工具全绿、fail-closed、旧行守恒 |
| P4 后端切换 | `DONE` | 见上 P4 完成记录：11 commits、auth 四端点 + Bearer + 全链路 user_id、X-Device-ID 退出、557/0 四工具全绿 |
| P5 LLM 后台 user_id 接续 | `DONE` | 见上 P5 完成记录：6 判别测试（logout/过期继续、session 零依赖、跨用户 404、metrics 无身份）+ 资产只读，563/0 四工具全绿 |
| P6 Android | `DONE` | 见上 P6 完成记录：4 commits 60d62a3..f15457b、Keystore 会话存储 + Bearer + UI + 零残留、40/40 + assembleDebug 全绿 |
| P7 test-platform v2 | `DONE` | 见上 P7 完成记录：3 任务、client 账号化 + 4 场景 + 成本闸门预算推导/批次对账、77/0 + unittest 6/6、CLI 形状实测 |
| P8 总验收 | `DONE` | 563/0 四工具全绿 + alembic 6 revisions 往返零漂移 + sentinel 扫描干净 + WORKER_REPORT.md 落盘（未运行项如实声明） |

## 变更纪律

- 本文件只记录实际状态和证据，不把计划勾选当完成。
- 不覆盖用户差量；发现与当前任务重叠的未提交文件时先停在 P0 核对。
- 不在未获授权时部署、push、迁移生产库，不迁移/认领/删除旧 device_id 数据（清理属后续单独发布）。
- 每个阶段完成、验证完成、策略变化或上下文退出前更新本文件。
- 两份上游设计（LLM spec/plan、账号 DESIGN）只读；实现冲突先归因、记录，再按设计忠实性处理。

## 最终门禁

- [x] P1 完成：17 任务真实证据 + canary 通过 + Progress 登记（R-03 RESOLVED）+ `LLM_BASELINE_COMMIT`。
- [x] V2.2 正式契约与实现一致。
- [x] X-Device-ID 已退出普通运行时认证/授权面。
- [x] 用户隔离、会话、幂等与敏感数据测试通过。
- [x] Planner/Generator/Scoring/Rewrite 与 ledger 已按 user_id 接续（语义冻结不回改）。
- [x] Android 与 test-platform v2 已完成并验证（P6：40/40 + assembleDebug；P7：77/0 + unittest discover 6/6 + CLI 形状与闸门实测）。
- [x] 全量质量工具真实通过，未运行项明确报告。
- [x] `WORKER_REPORT.md` 已记录改动、命令、退出码、计数、风险与未完成项。

未全部满足时不得把本 Goal 报告为完成。
