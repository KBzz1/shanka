# LLM 链路升级 + 账号登录替代设备 ID 与测试平台扩展：长程任务合并设计

## 0. 元信息与状态

- Goal ID：`shanka-llm-account-long-run-v1`
- 日期：2026-08-13
- 状态：`DESIGN_FROZEN`（合并引用定稿；被引用上游设计各自 FROZEN）
- 项目根：`/home/kbzz1/shanka_backend`
- 本文件职责：把两个既有设计合并为一个长程任务包的目标、顺序、安全边界和完成定义；**正文不复制
  上游设计全文**，只做导航引用与合并语义。
- 执行入口：同目录 `WORKER_PROMPT.md`；任务地图：同目录 `TASKS.md`；状态账本：同目录 `STATUS.md`。

### 上游设计权威（只读引用，禁止修改）

| 引用 | 路径 | 职责 |
| --- | --- | --- |
| LLM 短程任务设计 | `docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md` | LLM 升级需求/契约/迁移 0003/验收（§1–§14） |
| LLM 实施计划 | `docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md` | 17 任务 TDD 步骤、失败测试、提交信息（唯一步骤权威） |
| 账号长程任务设计 | `docs/account-auth-test-platform-long-run-v1/DESIGN.md` | 账号契约/归属迁移/Android/test-platform v2（§1–§13；§4.4 的 legacy-claim 端点与 §5.3 的 claim ticket/旧数据认领**不在本包执行范围**，用户 2026-08-13 决策） |
| 账号历史状态 | `docs/account-auth-test-platform-long-run-v1/STATUS.md` | 历史快照；已被本包接管，保留为只读记录 |

本设计不是任何功能已经实现的证明，也不替代未来落入 `docs/PRD/` 与 `docs/Architecture/` 的正式契约。

## 1. 最终目标与非目标

### 1.1 最终可检查目标

**目标一（LLM 链路升级，先行）：** 激活并升级 LLM 全链路——按文件页码持久化文本 →
LLM 规划 → 单元锚定生成 → LLM 评分；任务创建异步化，批=单元，`llm_call_attempts` 账本为
重试/上限/成本权威，估算删除，契约同步，前端直改。细节以 LLM spec 与 plan 为准。

**目标二（账号，后行）：** 完成面向单用户/小规模部署的最简账号系统，并把现有设备身份链路完整
切换为账号身份：

1. Android 端可注册、登录、保持会话、退出；业务请求使用 `Authorization: Bearer <opaque-token>`。
2. `X-Device-ID` 不再是任何业务接口的认证、授权、幂等、限流或数据归属依据。
3. 牌组、卡片、PDF、任务、复习、统计、DeepSeek API Key、LLM 调用账本全部按 `user_id` 隔离。
4. Planner → Generator → Scoring 与 Rewrite 后台链路在无 HTTP 会话上下文时仍能按 `task.user_id`
   找到所属数据和 API Key；会话退出/过期不取消已创建任务。
5. 旧 device_id 数据不做迁移/认领（用户 2026-08-13 决策）：原样保留在库，不再有访问路径；
   清理/下线属后续单独发布，需另行批准。
6. 扩展 `test-platform/`，覆盖账号安全、用户隔离、核心业务、三阶段 LLM 流水线与真实成本闸门。
7. PRD、Architecture、OpenAPI、ORM、迁移、前端对接和测试平台契约一致，并有聚焦与全量验证证据。

### 1.2 明确非目标

- 不实现邮箱/短信验证、第三方 OAuth、MFA、验证码、角色权限、组织/管理员后台。
- 不实现密码找回、邮件重置、refresh token、跨区域多实例会话系统。
- 不实现旧数据认领/迁移：claim ticket、legacy 表复制、事件 ID 重映射、API Key 密文搬运一律不做
  （用户 2026-08-13 决策）。
- 不改变 Planner/Generator/Rewrite/Scoring 的提示词内容、输出 Schema、Rubric、配额、状态机或质量口径
  （目标一冻结后即语义冻结；目标二只做归属与凭据解析改造）。
- 不进行生产部署、生产数据库迁移、旧数据删除、旧兼容入口下线、push 或 PR。
- 不因一次机器负载导致的测试耗时而另立性能改造；只记录命令、退出码和真实耗时。
- 不创建覆盖 F0～R1 的并列总计划；`docs/Progress.md` 仍是范围/依赖/状态/DONE 事实源。

## 2. 顺序、门禁与并行纪律

### 2.1 防干扰顺序

```text
P0 现场基线核验
   ↓
P1 LLM 升级实施（17 任务，plan 为唯一权威）→ 冻结 LLM 基线
   ↓
P2 契约 V2.2（PRD + Architecture/OpenAPI/DB 同步）
   ↓
P3 数据地基（users/sessions/用户归属迁移，无 legacy claim）
   ↓
P4 后端切换（auth、Bearer、所有权、幂等、限流）
   ↓
P5 LLM 后台 user_id 接续（不改变 LLM 语义）
   ↓
P6 Android 登录切换
   ↓
P7 test-platform v2
   ↓
P8 总验收（四工具、Android、迁移副本、受控 canary）
```

P0/P1 属于目标一，P2～P8 属于目标二；顺序不可颠倒（账号契约依赖 LLM 基线，账本归属迁移依赖
真实 0003 head）。

### 2.2 P1 完成门禁（LLM 基线冻结，替代原账号 Gate 0）

目标二第一次写共享文件（`docs/PRD/`、`docs/Architecture/`、`main/` 账号部分、
`frontend-app/Front/`、`test-platform/`、`agent_evolution/`）前，必须同时满足：

1. LLM 17 任务已按 plan 完成，每任务有真实命令证据（失败测试→实现→四工具/回归），
   不是 manifest 切换或 plan checkbox；
2. 受控真实 canary 通过（本机 `.env` 加载 Key，权限 600、git 忽略），预算上限以内、账本为准；
   `docs/Progress.md` 登记 `LOCAL_IMPLEMENTATION_DONE` → `PRODUCTION_VALIDATED`，R-03 RESOLVED；
3. 记录 `LLM_BASELINE_COMMIT=$(git rev-parse HEAD)` 与真实 Alembic head（预期含 `0003_*`）；
4. 碰撞区没有本包外的未提交修改（用户差量保护清单仍成立）。

原账号设计的"等待外部 worker 交接"语义随任务合并取消；本包内同一 Worker 串行执行，门禁变为
内部阶段闸门。

### 2.3 并行纪律

- 普通顺序工作直接完成；只有独立且重上下文的读取/核验岛才使用最少 subagent（如只读核对迁移守恒、
  实现完成后新上下文只读检查认证/敏感边界）。
- 不得让多个 agent 并行修改 PRD、Architecture、ORM、OpenAPI 或共享测试；有重叠时由主 Worker 统一落盘。
- 每个任务按 superpowers 技能 `subagent-driven-development` 实现（独立 subagent 逐任务执行，
  主 Worker 统一集成与验收）；实现 subagent 不修改 PRD、Architecture 或 Progress，整包验收后由
  主 Agent 更新 Progress。

## 3. 全局约束（合并红线）

工程：

- 执行环境 `cd main && /home/kbzz1/miniconda3/bin/conda run -n shanka-backend python -m pytest`；
  四工具全绿：pytest / `ruff check .` / `ruff format --check .` / `mypy .`（line-length 100、mypy strict）。
- Python 依赖与 lint 配置唯一事实源 `main/pyproject.toml`；配置 pydantic-settings 单层，密钥走环境变量。
- 依赖方向单向向下：`docs/PRD → docs/Architecture → main/`；实现不得反向驱动契约。

LLM（目标一冻结，目标二不得回改）：

- 每单元 1 卡（N=1 固定）；密度只控制单元预算上限（每章 3×密度系数，COMPACT=1/BALANCED=2/EXTENSIVE=3）。
- 配额三层最大余数法，固定顺序消除随机性；例：预算 6、40/40/20 → 3/2/1。
- LLM 调用必须在事务外发起；任何外部 chat 调用前必须先有已提交的 STARTED 账本行。
- 任务状态机 PENDING(PLANNING) → RUNNING(PLANNING) → RUNNING(GENERATING) → RUNNING(SCORING) →
  COMPLETED/FAILED/CANCELLED；PAUSED 仅由 resume 恢复。
- 空单元三分支：全成功 0 单元 → COMPLETED + `NO_GENERATION_UNITS`；全失败 → FAILED+PLANNING；
  部分失败 → 成功组继续 + `skipped_planning_group_count`。
- 成本口径：账本是全阶段 token 唯一来源；Batch token 仅 GENERATING 兼容投影；quality-summary
  `cost_estimate` 标注 generation-stage only，禁止双计。

账号与安全：

- 红线 4：API Key 只出现在 `infra/llm/` 调用路径；任何日志、响应、任务明细不得引用明文；llm 层异常
  统一脱敏为 `API_KEY_*` / `GENERATION_FAILED`。
- 红线 5：manifest ↔ structure-contract 版本一致；已发布资产目录禁止原地修改（新版本 = 新目录 +
  manifest + CHANGELOG 同次提交）。
- 密码 Argon2id 生产参数不低于 `memory_cost=19456 KiB, time_cost=2, parallelism=1`；测试可注入低成本
  hasher，不得降低生产默认或配置守卫。
- 256-bit opaque bearer token，DB 只存 SHA-256 摘要；30 天绝对有效期，无 refresh token。
- 显式 `AuthPrincipal(user_id, session_id)`；跨用户资源统一 404；X-Device-ID 退出普通认证/授权/幂等/
  限流/归属（历史文档、迁移与 legacy fixture 保留字样可以）。
- `Authorization`、密码、token/hash、legacy_device_id、API Key、完整 Prompt 与原始模型响应不得进入
  日志、错误、测试报告或命令参数。

数据与迁移：

- 新写入必须带 user_id 且不得生成 device_id；历史 device_id 行原样保留、不迁移不认领、无访问路径
  （用户 2026-08-13 决策：不做老数据迁移）。
- 只在空库/副本做 upgrade→downgrade→upgrade；存在新用户数据时 downgrade fail closed，回退只恢复
  升级前 DB + storage 备份。本任务不授权生产迁移。
- 不物理删除旧列、旧表或历史行；不做任何数据搬运或 API Key 密文复制；清理/下线是需用户另行批准的
  后续发布。

## 4. 设计正文（导航引用）

### 4.1 目标一：LLM 链路升级设计

以 `docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md` 为唯一设计权威，执行细节以
`docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md` 为唯一步骤权威。关键节导航：

- §1 背景与目标（现状缺陷与总原则：代码决定数量/配比/上限/ID/状态机；LLM 只产出锚定生成单元）
- §3 概念模型（生成单元、双维锚定、组合规则、密度与预算、难度配额）
- §4 按文件页码持久化文本（text_chunks；规划时冻结章节最新页码并选页）
- §5 LLM 资产与输出契约（v3/v2 版本布局、Planner/Generator/Scoring/Rewrite 契约、输出校验、Prompt
  组装与不可信输入隔离、资产验收矩阵）
- §6 任务状态机（PLANNING 阶段、CAS 抢占与快照冻结、规划执行、错误分类与重试、空单元语义、
  cancel/resume/删除保护）
- §7 批关联与观测（批=单元）
- §8 Scoring（独立 SCORING 阶段 + 分层抽样）
- §9 `llm_call_attempts` 调用账本（新表）
- §10 估算接口删除与全局硬上限
- §11 数据库迁移（0003）
- §12 契约同步（structure-contract / PRD / database-design / openapi / 红线 5 版本）
- §13 测试与验收；§14 登记与风险

### 4.2 目标二：账号登录与测试平台设计

以 `docs/account-auth-test-platform-long-run-v1/DESIGN.md` 为唯一设计权威（含其自审表与残余风险
接受边界）。**注：其 §4.4 的 legacy-claim/ticket 端点、§5.3 的 claim ticket 与旧数据认领不在本包
执行范围**（用户 2026-08-13 决策，见 §1.2 非目标）。关键节导航：

- §1 最终目标与非目标；§2 已核实现场；§3 防干扰顺序与 Gate 0（被本文件 §2.2 改写为内部门禁）
- §4 冻结的最简账号契约（V2.2、用户与凭据、会话、最小 HTTP 接口、限流/日志/敏感信息）
- §5 数据归属与迁移设计（新归属模型、Alembic 策略；一次性 claim ticket 与旧数据认领不在本包范围）
- §6 Planner、生成、评分与重写的账号化（只改归属与凭据解析，不改变 LLM 语义）
- §7 Android 最小登录体验
- §8 测试平台 v2（核心库与 runner、场景地图、LLM 成本闸门）
- §9 分阶段执行与 checkpoint（原 P1～P7 在本包整体后移为 P2～P8）
- §10 最小验证矩阵；§11 自审修正与残余风险
- §12 完成定义；§13 安全依据

### 4.3 两段衔接语义（合并点）

1. **归属桥**：目标一账本 `llm_call_attempts` 先按 `device_id` 落成（0003）；目标二 P3/P5 在其真实
   head 上追加用户域迁移，账本归属改 `user_id`；旧 `device_id` 列/行保留、新写入不生成（不做认领/搬运）。
2. **语义冻结**：P1 完成后 v3/v2 prompt、schema、rubric、manifest、配额、状态机与质量口径冻结；
   P5 只做归属与凭据解析，不得回改语义或版本。
3. **完成口径分层**：目标一按 LLM plan 的 LOCAL_IMPLEMENTATION_DONE → PRODUCTION_VALIDATED 登记
   Progress/R-03；目标二按账号设计完成定义登记；两者互不替代。
4. **成本闸门**：目标一 canary 与目标二 test-platform live 均为受控真实调用，预算上限 + 账本对账 +
   单独成本确认；未运行不得以 mock 代替宣称。

## 5. 完成定义（合并）

只有下列全部成立才可报告本 Goal 完成：

**目标一（LLM）：**
1. 17 任务按 plan 全部完成，四工具/守卫/迁移检查真实通过（退出码 0，警告单独登记）；
2. 受控真实 canary 通过且预算上限内；`docs/Progress.md` 登记完成口径，R-03 RESOLVED；
3. `LLM_BASELINE_COMMIT` 与真实 Alembic head 已记录。

**目标二（账号）：**
4. V2.2 PRD 与 Architecture/OpenAPI/DB 契约一致并通过守卫；
5. 后端、Android、test-platform 普通运行路径均不再用 X-Device-ID 认证或授权；
6. 新账号归属迁移测试通过（副本 upgrade→downgrade→upgrade + 新数据 downgrade fail closed），且未
   执行生产迁移/删除、未迁移/认领/删除任何旧 device_id 数据；
7. 所有资源与 LLM ledger 按 user_id 隔离；后台三阶段任务在 session 撤销后可继续；
8. 聚焦、全量、静态检查、迁移、Android 与平台测试真实通过；受控 live 若未获 Key/成本批准，必须
   明确标为未运行，不能用 mock 代替宣称；
9. `STATUS.md` 与最终 `WORKER_REPORT.md` 写明实际文件、命令、退出码、计数、残余风险和未完成项；
10. 未覆盖用户现有差量，未修改两份上游设计或 LLM v3/v2 资产语义，未部署、push，未迁移/认领/删除
    旧 device_id 数据。

## 6. 安全依据

- OWASP Password Storage Cheat Sheet（Argon2id 及最低参数基线）：
  <https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html>
- RFC 9106（Argon2 参数与安全背景）：<https://www.rfc-editor.org/rfc/rfc9106.html>
- OWASP Session Management Cheat Sheet（会话 token 随机性、生命周期与泄漏边界）：
  <https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html>
- LLM spec §13/§14（测试验收与登记）；账号设计 §11（攻击假设后的自审修正与残余风险接受边界）。
