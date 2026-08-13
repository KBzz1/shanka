# Worker 启动提示词：LLM 链路升级 + 账号登录替代设备 ID 鉴权与测试平台扩展

你是本长程任务包的主执行 Worker。本任务包合并两个串行目标：**目标一（先）= LLM 链路升级**，
**目标二（后）= 账号登录替代 X-Device-ID**。请在真实仓库中自主读取、计划同步、实施、测试、修复和
记录 checkpoint；不要只复述设计或重新生成一份平行 plan。任务可跨会话恢复，但每次都从已验证证据
继续，不得从文件名、提交信息或计划勾选推断实现完成。

## 项目与最终目标

- 项目根：`/home/kbzz1/shanka_backend`
- 任务包：`/home/kbzz1/shanka_backend/docs/llm-account-long-run-v1/`
  - 冻结设计：`DESIGN.md`（引用两份上游设计，不复制正文）
  - 任务地图：`TASKS.md`（勾选仅供进度跟踪，不等于完成）
  - 状态账本：`STATUS.md`
- 目标一（LLM 链路升级）：按 `TASKS.md` 第 1 阶段的 17 个任务激活并升级
  Planner → Generator → Scoring / Rewrite 全链路（文本页持久化 → LLM 规划 → 单元锚定生成 →
  LLM 评分），任务创建异步化，批=单元，账本权威，估算删除，契约同步。
- 目标二（账号）：建立 PRD V2.2 的最简用户名/密码账号系统，用 opaque Bearer session 完整替代
  `X-Device-ID` 认证；所有业务、幂等、统计、API Key 和 LLM 后台链路按 `user_id` 归属；Android 与
  `test-platform/` 同步切换并通过与风险匹配的验证。**不做旧数据认领/迁移**（用户 2026-08-13 决策）：
  旧 device_id 数据原样保留、不迁不删、无访问路径。

先完整读取根 `AGENTS.md`、任务包内全部文档、被引用的两份上游设计与 LLM plan/spec，以及目标路径内
所有适用 `AGENTS.md`。项目规则的依赖方向、Conda 环境、API Key 脱敏、契约同步和用户差量保护均为硬边界。

## 上游设计权威（只读引用，禁止修改）

1. LLM 短程任务设计（FROZEN）：
   `docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md`
2. LLM 实施计划（TDD 步骤唯一权威）：
   `docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md`（17 任务、失败测试、提交信息）
3. 账号长程任务设计（FROZEN）：
   `docs/account-auth-test-platform-long-run-v1/DESIGN.md`
4. 账号历史状态：`docs/account-auth-test-platform-long-run-v1/STATUS.md`（已被本包接管，保留为历史）。

## 已核实现场与上游输入

1. 工作树存在 LLM 升级施工中的未提交差量（17 个修改 + 未跟踪资产，清单见 `STATUS.md` 现场快照）。
   这是本包 P1 的施工中产物，不是外部任务；不得视为已完成，也不得覆盖丢弃。
2. 聚焦测试 `45 passed, 1 warning`（2026-08-12/13 两次复核）只证明当前 v3/v2 资产与已接线增量的
   聚焦测试通过，不证明 LLM 三阶段升级完成。
3. 设计期快照 HEAD `8cd0cb5010316359e45003a985ddab2fe35c6188`、Alembic head `ead86a96d103`；
   该快照可能已过期，执行时必须重新核对。
4. 当前 PRD v2.1 明确排除账号并由 D-02 决定设备身份；破坏性变更必须建立 V2.2 需求权威，不能
   直接让实现反向改写 v2.1。
5. 设备 ID 当前贯穿 middleware、handlers、services、ORM、幂等、日志、Android 和 test-platform；
   目标二是全栈身份/归属迁移，不是只换一个 header。
6. 当前 shell 可能找不到短命令 `conda`；使用
   `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`。Python 依赖仍只以
   `main/pyproject.toml` 为事实源，不装入 base/系统 Python。

## 第一动作：P0 现场核验门禁

重新读取 `git status --short --branch`、`git diff`、两份上游设计、LLM plan、manifest、实际三阶段实现、
测试结果、`git rev-parse HEAD` 和 `python -m alembic heads`，并完成：

1. 核对未提交差量与 LLM plan 各 Task 的一致性，列出哪些 Task 已有施工、哪些缺失；确认用户差量
   保护清单（属于用户/本包施工但未提交的文件一律保留，不得 restore/reset/覆盖）。
2. 记录执行时 `HEAD`、Alembic head（预期 LLM 升级前仍为 `ead86a96d103`，升级后为 `0003_*`）。
3. 以真实命令证据（pytest 失败→通过、四工具、迁移检查）确认从当前现场继续 P1 的可行性；缺什么
   按 plan 的 TDD 步骤补齐，不重做已核实阶段。

P1 完成门禁（LLM 基线冻结，之后才可写账号碰撞区 `docs/PRD/`、`docs/Architecture/` 账号部分等）：

- LLM 17 任务全部按 plan 完成，且每任务有真实命令证据（失败测试→实现→四工具/回归通过），
  不是 manifest 切换或 plan checkbox；
- 受控真实 canary 通过（本机 `.env` 加载 Key、权限 600、git 忽略），预算上限以内，账本为准；
  `docs/Progress.md` 登记 `LOCAL_IMPLEMENTATION_DONE` → `PRODUCTION_VALIDATED`，R-03 RESOLVED；
- 记录 `LLM_BASELINE_COMMIT=$(git rev-parse HEAD)` 与真实 Alembic head；
- 上述碰撞区不存在另一任务仍在修改的未提交文件。

若 P0 发现不可恢复的矛盾（如差量与 plan 严重漂移）：不要在 `STATUS.md` 之外乱写，追加证据后回传
`BLOCKED_ON_LLM_WORKTREE` 并给出唯一恢复动作。

## 必须完成的结果

### 目标一：LLM 链路升级（先做，17 任务）

按 `TASKS.md` 第 1 阶段与 LLM plan 逐任务执行；每个任务使用 superpowers 技能
`superpowers:subagent-driven-development`（独立 subagent 逐任务实现，主 Worker 负责集成与验收），
步骤用 plan 的 TDD 顺序（写失败测试 → 运行确认失败 → 实现 → 四工具全绿 → 按提交信息 commit）。要点：

- 代码决定数量/配比/上限/ID/状态机；LLM 只产出带"学习目标+目标难度+锚定卡型+来源引用"的生成单元。
- 规划快照在首次 CAS1 抢占时原子冻结；所有 LLM 调用先持久化 STARTED 占位（`llm_call_attempts`
  账本为重试/上限/成本权威）；领域写入与调用终态同事务；PLANNING → GENERATING → SCORING 三阶段，
  全部条件更新防并发。
- 全局硬约束（见 DESIGN.md §3）：每单元 1 卡、三层最大余数法配额、事务外调用、空单元三分支、
  成本口径 generation-stage-only、完成口径 LOCAL_IMPLEMENTATION_DONE → PRODUCTION_VALIDATED。
- 前端直接修改 `frontend-app/Front/` 源码，不新增 `docs/frontend/handoff/*`，不 fork/push。
- 不修改 spec/plan 文本来迁就实现；实现与文档冲突时先归因、记录，再按设计忠实性处理。

### 目标二：账号登录与测试平台（后做，8 节）

以下 8 节继承自账号长程任务设计（`docs/account-auth-test-platform-long-run-v1/DESIGN.md`）的
冻结内容，与目标一合并后按 P2～P8 顺序执行。

#### 1. 先建立正式 V2.2 契约

- 新建 `docs/PRD/V2.2/prd_v2_2.md`，继承 v2.1 的业务需求并明确账号会话取代 D-02；不要篡改
  PRD V2.1 的历史决策。
- 冻结设计规定的注册、登录、退出、当前用户和一次性 legacy claim 行为、排除项、风险提示与验收标准。
- 原子同步 `docs/Architecture/structure-contract.md`、`openapi.yaml`、`database-design.md`、前端对接、
  错误码/文案和 contract guards；更新当前 PRD 链接与 `docs/Progress.md` 时只报告真实状态。
- 账号 HTTP 契约固定为：`POST /auth/register`、`POST /auth/login`、`POST /auth/logout`、
  `GET /auth/me`；无 legacy claim 端点。X-Device-ID middleware 与设备身份退出普通认证/授权/幂等/限流。
  错误码最小集合不含 `LEGACY_CLAIM_UNAVAILABLE`。

#### 2. 实现最简、安全的账号与会话

- 用户名 3～32 位，规范化为 ASCII 小写，只允许 `[a-z0-9._-]`；密码 8～128 字符，不静默截断、
  不做 Unicode normalization 或大小写转换。
- 密码使用 Argon2id；生产参数不得低于 `memory_cost=19456 KiB, time_cost=2, parallelism=1`。测试可以
  注入低成本 hasher，但不能降低生产默认或配置守卫。
- 使用 256-bit 随机 opaque token；数据库只存 SHA-256 摘要；默认 30 天绝对有效期，无 refresh token。
- register/login 成功返回用户最小资料、token 与 expires_at；logout 只撤销当前 session；`/auth/me`
  验证当前 session。
- 登录失败统一 `401 INVALID_CREDENTIALS`；用户名冲突 `409 USERNAME_TAKEN`；无效/撤销/过期会话
  统一 401 并带 `WWW-Authenticate: Bearer`。
- 用户名不存在时仍做固定 dummy Argon2id 校验，降低账号存在性时序差；不要写精确毫秒门槛测试。
- register/login 成功形状使用 `user`、`access_token`、`token_type="Bearer"`、`expires_at`；client 不自动
  重试这两个请求。账号错误码按账号设计的最小集合进入统一 ErrorResponse。
- 建立显式 `AuthPrincipal(user_id, session_id)` 或等价对象；跨用户资源继续统一 404。
- register/login 按 IP 限流，login 还按规范化用户名限流；认证后业务限流按 user_id 并保留 IP 总闸门。
- `Authorization`、密码、token/hash、legacy_device_id、API Key、完整 Prompt 和原始模型响应不得进入
  日志、错误、测试报告或命令参数。敏感路径要在服务端与 test-platform client 统一脱敏。

#### 3. 完成用户归属迁移（不做老数据认领/迁移）

- 直接 owner roots 改为 `user_id`：API Key、PDF、Task、Deck、Card、ReviewEvent、
  LlmCallAttempt；幂等域改为 `(user_id, path, idempotency_key)`，复习事件客户端键改为
  `(user_id, client_event_id)`。
- Chapters/TextChunks 经 PDF，规划单元/KnowledgePoint/Batch 经 Task，ReviewState 经 Card 传递归属。
- 补齐 user_id 查询索引，并用服务/测试守住 Card↔Deck、Task↔PDF/Deck、LlmCallAttempt↔Task 的
  user_id 一致性，不能只验证关联资源存在。
- handlers/services 使用 user_id/principal；禁止登录后反查一个"主 device_id"继续作为租户键。
- 根据 P1 门禁记录的真实 migration head 创建下一 revision，不硬编码 `0004`。新表最小字段与索引：
  users、auth_sessions（无 devices claim 元数据）；owner 表加 user_id 列——新写入必须非空且不得
  生成 device_id，历史 device_id 行原样保留。
- 旧 device_id 数据不做迁移、认领或删除（用户 2026-08-13 决策）：保留在库但不再有访问路径；
  不做 API Key 密文搬运；清理/下线是需要用户另行批准的后续发布。
- 只在空库/副本做 upgrade→downgrade→upgrade；存在新用户数据时 downgrade 必须在写入前 fail
  closed，回退只能恢复升级前 DB + storage 备份。本任务不授权生产迁移。
- 不物理删除旧列、旧表或历史行。

#### 4. LLM 后台链路按 user_id 接续（不改变 P1 已冻结的 LLM 语义）

- `tasks.user_id` 是 Planner/Generator/Scoring 后台执行身份；executor 不持有 bearer token。
- Samples、Planner/Generator/Scoring/Rewrite 的 API Key 与 `llm_call_attempts` 按 user_id；受保护的
  quality-summary/observability/成本按 user_id。匿名 `/metrics` 只含无身份、无资源 ID 的系统聚合。
- logout 或 session expiry 后，已创建任务继续运行；重新登录后可查看、取消、恢复。
- operation_key、input_fingerprint、CAS、重试、配额、状态机均不得依赖 session/token；会话轮换不使账本
  恢复结果失效。
- 本阶段只做归属与凭据解析改造，不得回改 P1 冻结的 v3/v2 prompt、schema、rubric、manifest 语义、
  版本或状态机；`llm_call_attempts.device_id` 列保留但不再有新写入（历史行原样保留）。

#### 5. 完成 Android 登录切换

- 增加最简 Login/Register 状态和界面；启动时用安全存储 token + `/auth/me` 决定进入主应用或登录页。
- 用 Android Keystore 支持的加密会话存储替代 `SecureDeviceIdentityStore`；密码不持久化。
- 网络层对受保护请求加 Bearer，register/login 不加，普通请求彻底移除 X-Device-ID。
- 服务端明确 401 时清会话并回登录页，不能无限重试；网络失败不能误判为退出。
- 无旧数据认领提示（不做 legacy claim）。
- 不扩展找回密码、头像、资料页、OAuth/MFA。

#### 6. 把 test-platform 扩展为账号与三阶段流水线验证平台

- 保持纯 stdlib 与黑盒 HTTP；client 支持 register/login/set_token/logout 和统一敏感路径脱敏。
- runner 删除 `--device-id`。既有测试账号凭据只从 `SHANKA_TEST_USERNAME` / `SHANKA_TEST_PASSWORD`
  读取；不得出现在 CLI、console、JSONL。prod 必须显式确认且禁止自动注册/legacy claim。
- 实现设计中的 auth、isolation、cards/review/stats、pdf、generation、observability 最小场景；
  不创建无行为占位文件（无 legacy 场景）。
- quick/full 默认不调用真实 LLM；live 用受控小夹具覆盖 PLANNING → GENERATING → SCORING、ledger、
  空规划/部分失败/取消恢复等承重语义。
- 删除"live 固定 3 次调用"假设：运行前用 fixture 与配置推导最坏调用预算，运行后以该用户的
  `llm_call_attempts` 对账实际 attempts/token/成本。真实调用需单独成本确认；未运行就明确写未运行。
- 场景结束清理业务资源与 session；无法安全删除的 local 测试 user 行按 run_id 计数报告，不为清理方便
  擅自增加危险的生产账号删除接口。

#### 7. 持续状态与最终报告

- P0～P8 每个阶段完成、验证完成、策略改变或会话退出前更新 `STATUS.md`，记录具体命令、退出码和
  checkpoint；不重做已核实阶段。
- 完成时在同目录创建 `WORKER_REPORT.md`，列实际改动、迁移计数、测试命令/结果、未运行验证、残余风险
  和未完成项。提示词/设计/报告存在均不等于实现完成。

## 禁止项

- 不覆盖、restore、reset、stash 或提交用户已有修改；不使用破坏性 Git/文件命令。
- 不部署、push、开 PR、迁移生产库，不迁移/不认领/不删除旧 device_id 数据，不触发未确认的付费 LLM 调用。
- 不修改两份上游设计（LLM spec/plan、账号 DESIGN）或 v3/v2 资产语义来"顺便完成"或迁就实现；
  上游文件只读。
- 不编辑 PRD V2.1 的历史 D-02；新需求只能进入 V2.2。
- 不使用 JWT、明文/可逆密码、明文 session token 落库、API Key 明文复制或弱化生产 Argon2id 参数。
- 不让 X-Device-ID 继续参与普通认证、授权、幂等、限流或归属；历史迁移/legacy fixture 中保留字样可以。
- 不为本任务引入 OAuth、邮件、MFA、RBAC、外部队列、通用 Repository/DI 框架或多实例会话设施。
- 不把 mock/fake 验证报告成真实 DeepSeek canary、Android 真机或生产迁移。
- 不创建覆盖 F0～R1 的并列总计划；进度登记仍以 `docs/Progress.md` 为范围事实源（LLM 段按 plan
  T14/T17 登记，账号段按真实状态登记）。

## 执行与上下文隔离

你持有设计忠实性、共享文件修改、集成和验收责任。普通顺序工作直接完成；只有存在独立且重上下文的
读取/核验岛时才使用最少 subagent，例如让一个只读 subagent 核对数据库迁移守恒，或在实现完成后让
一个新上下文只读检查认证/敏感信息边界。不得让多个 agent 并行修改 PRD、Architecture、ORM、OpenAPI
或共享测试；有重叠时由你统一落盘。subagent 回传只保留结论、文件/行证据、测试和风险。

每个任务按 superpowers 技能 `superpowers:subagent-driven-development` 实现（独立 subagent 逐任务
执行，主 Worker 集成）；实现 subagent 不修改 PRD、Architecture 或 Progress，只有整包验收
通过后由主执行 Agent 更新 Progress（沿用 `docs/superpowers/plans/CLAUDE.md` 规则）。

## 验证要求

至少完成并记录：

1. LLM 段：每个任务 TDD 四工具证据；P1 门禁真实 canary（Planner→Generator→Scoring 单任务全链路，
   预算上限内，账本 token 为准）；`alembic upgrade head` / `alembic downgrade 0002` 往返 + `alembic check`
   零漂移。
2. 账号聚焦 unit/integration/contract/acceptance：注册登录、Argon2 参数、session 撤销/过期、两用户隔离、
   幂等域、复习 client_event、敏感脱敏。
3. 空库/副本做 upgrade→downgrade→upgrade；存在新用户数据时验证 downgrade fail closed；
   `alembic check`、计数/FK/索引守恒。PDF storage 前后按 file_id/storage_key/size/SHA-256/readable
   建 manifest，验证无新增 missing/orphan。
4. 后端全量：在 `main/` 用 `shanka-backend` 环境运行 pytest、ruff format check、ruff check、mypy。
5. `test-platform/` 自测、local quick/full；live 只有成本/Key 条件满足并获确认才运行，报告 ledger 实际值。
6. Android 至少运行相关 JVM tests、`assembleDebug` 与可用的静态/编译检查；无真机时明确未运行 instrumented。
7. 限定运行时代码的静态守卫：普通 middleware/services/OpenAPI/Android client/test-platform client 不再用
   X-Device-ID 作为身份；历史文档、迁移与 legacy 测试不要求全仓零命中。
8. 日志、测试输出和报告敏感扫描：为密码、token、legacy ID、API Key、Prompt 注入唯一 sentinel，扫描
   这些具体值是否泄漏；不要用 `password`/`token` 等通用字段名做全仓零命中，也不要把 DB 中应有的
   password hash 列误报为泄漏。
9. 后台测试：创建任务后 logout，executor 仍按 user_id 跑完；新 session 可读取；跨用户 ledger/task 404。

测试耗时增加本身不是失败证据。以进程退出码、失败栈、资源观测和可复现性判断；遇到失败先做最小诊断，
改变策略后复验，不原样重复。

## 停止与最终回传

只有以下情况才停止未完成任务：P0 发现真实并发碰撞或差量不可恢复漂移；需要生产迁移/删除/部署/付费
调用的新授权；冻结设计与真实约束存在会改变产品语义的矛盾；或安全替代路径已穷尽。停止时更新
`STATUS.md`，给出精确证据、已尝试安全路径和唯一恢复动作。

最终简洁回传：完成了什么；实际修改路径；每条关键测试命令及结果/退出码；迁移与场景计数；未运行项；
残余风险/阻塞。两个目标的完成定义（`DESIGN.md` §5）未全部满足时，不得宣称本 Goal 完成。

现在开始执行。
