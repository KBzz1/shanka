# Email 登录 + 错误提示 UX + 长期登录 + 平台 backlog：设计规格

- 日期：2026-08-14
- 状态：用户已确认设计（脑暴流程三部分全部确认；倒计时归零时整个错误提示框消失的语义已并入 §4.2）
- 上游权威：本文件为工作包设计规格；契约变更按契约驱动流程同步 PRD（升 V2.4）/Architecture

## 0. 背景与决策记录

来源：T14 收尾后用户新增需求（ledger「T14 后新需求」记录 5 条）+ final review b 类 backlog 2 条。
「直接进入」死按钮经澄清裁决**不动**，不在本工作包范围。

用户裁决（2026-08-14 brainstorming，全部确认）：

1. 范围：全部 8 项（剔除死按钮后 7 项）一个 spec。
2. email 登录：users 表加 email 字段；注册收 username+email+password；登录用 email+password；
   username 用于主界面展示。存量测试账号**清空重来**（迁移清空账号及下游数据）。
3. username 定位为**展示用昵称**：格式放宽（1-24 字符，允许中文/字母/数字/._-，不强制小写）。
4. 长期登录 = **滑动续期**（活跃用户永不过期，连续不活跃仍过期）。
5. 错误映射 = **前端全量映射表**（后端全部错误码逐一映射，未知码兜底）。
6. 倒计时提示**仅登录/注册屏**（不做全 App 统一）；倒计时显示纯数字【3】【2】【1】（无省略号）；
   **倒计时归零时整个错误提示框消失**。
7. 注册表单**四字段保留**：用户名/邮箱/密码/确认密码（确认密码本地校验）。
8. 忘记密码文案改「请手动联系开发者直接修改密码。」。
9. 「直接进入」按钮维持现状不动。
10. 平台 429 修复、PLANNING 预算前提同步按 §5 设计。

## 1. 后端：email 登录（PRD V2.4）

### 1.1 users 表变更 + 迁移

- `users` 表新增 `email` 列：`String NOT NULL` + `UNIQUE`（约束名 `uq_users_email`），
  存服务端 lowercase 规范化值（登录键，必须唯一）。
- `username` 改为纯展示名：**移除 UNIQUE 约束**（允许重名，类似昵称）；字段仍 NOT NULL。
- 新迁移 revision（`down_revision` = 当前 head）：
  - `batch_alter_table` 给 users 加 email 列（SQLite batch 模式）。
  - **清空账号及下游数据**（用户裁决「清空账号表重新注册」）：按依赖序 DELETE
    auth_sessions → users，以及按 user 隔离的全部下游表
    （pdf_files/chapters/tasks/batches/knowledge_points/decks/cards/review_states/
    review_events/llm_call_attempts/api_keys/idempotency_keys）。
  - **downgrade 显式拒绝**（延续 V2.3 fail-closed 精神）：数据已清空不可逆，
    函数第一行 raise RuntimeError，回退请恢复升级前备份。
- 验证：空库 upgrade 全链 + `alembic check` 零漂移；开发库 `main/data/shanka.db` 实迁后
  账号表为空、email 列在位。

### 1.2 注册/登录契约

- `POST /auth/register`：`{ username, email, password }`
  - username：展示名，1-24 字符，允许中文/字母/数字/._-，trim 后非空；**不再强制小写**
    （normalize 只 trim；正则替换现 `^[a-z0-9._-]{3,32}$`）。
  - email：宽松格式校验（含 `@`、非空、长度上限 254）+ lowercase 规范化。
  - password：沿用 8-128。
  - 冲突语义：email 已占用 → `409 EMAIL_TAKEN`「邮箱已被占用」；username 重名不再报错。
- `POST /auth/login`：`{ email, password }`；email lowercase 规范化 + 宽松格式校验后查询；
  失败统一 `401 INVALID_CREDENTIALS`，文案改「邮箱或密码错误」（不暴露存在性；
  不存在先固定 dummy 校验抹平时序差——沿用现状机制）。
- 登录限流桶键：username → **email**（`rate_limit_auth_per_hour` 额度不变）；
  RATE_LIMITED → 429 + Retry-After 语义不变。
- `AuthUser`（3.14）形状不变（user_id/username/created_at），响应**不新增 email 字段**；
  前端 SessionUser 无需改。
- login 非法 email 格式按输入校验惯例返回 400 VALIDATION_ERROR（非 401），不泄露存在性。

### 1.3 错误码变更

- 新增 `EMAIL_TAKEN` = 409（注册 email 冲突）。
- 移除 `USERNAME_TAKEN`（username 去唯一后无此语义；全仓引用清零）。
- `INVALID_CREDENTIALS` 文案「用户名或密码错误」→「邮箱或密码错误」。

## 2. 后端：滑动续期（长期登录）

- 落点：auth 中间件（`main/app/middleware/auth.py`）——`resolve_principal` 成功放行前，
  若该会话 `expires_at` 距现在不足 29 天（即距上次续期已 ≥ 1 天），在同一 DB session 内
  UPDATE `expires_at = now + auth_session_ttl_days`（30 天）。
- 节流保证：只在 `expires_at < now + 29 天` 时写库 → 每会话每天至多一次 UPDATE，无写放大。
- 语义：
  - 活跃用户永不过期（「不登出就一直保持登录」）；
  - 连续 30 天无任何请求的会话仍自然过期（保留安全语义）；
  - 已撤销会话不续期（resolve 已过滤 revoked）；
  - logout 撤销语义（revoked_at）不变；register/login 新建会话不受影响。
- 配置：`auth_session_ttl_days` 沿用，无新配置项。

## 3. 前端：表单改造

- **登录屏**（AuthScreen.kt LoginScreen）：字段不变（label「邮箱」值即 email，本次语义对齐）；
  「忘记密码？」点击提示改「请手动联系开发者直接修改密码。」；错误反馈改用 §4 组件；
  登录按钮转场逻辑不变。
- **注册屏**（RegisterScreen）：四字段——用户名（展示名）/邮箱/密码/确认密码；
  - 确认密码**本地校验**：两次输入一致才允许提交，不一致即时用 §4 组件提示；
  - 密码 placeholder「至少 6 位」→「至少 8 位」（与后端 8-128 对齐）；
  - 提示卡「注册完成后请使用邮箱与密码登录。」保留（本次落定后语义真实成立）。
- 接线：`AppViewModel.register(username, email, password, confirmation, onResult)`；
  `AuthRepository.register(username, email, password)`；`RemoteFlashcardRepository` 透传三参数。
- 登录成功后主界面「用户名，快来学习」（HomeScreen.kt）展示 username——已接线，无需改。

## 4. 前端：错误映射表 + 倒计时提示组件

### 4.1 全量错误映射表

- `AuthViewModel.authErrorMessage` 从 4 路扩为**全量映射表**（独立文件/对象，便于单测）：
  后端全部错误码逐一映射中文文案（VALIDATION_ERROR / AUTH_REQUIRED / AUTH_INVALID /
  INVALID_CREDENTIALS / EMAIL_TAKEN / RATE_LIMITED / PDF_* / API_KEY_* / TASK_* /
  GENERATION_FAILED / DECK_* / CARD_* / IMPORT_* / REVIEW_* / IDEMPOTENCY_CONFLICT /
  INTERNAL_ERROR 等），文案与后端 message 语义一致。
- 未知错误码兜底「操作失败，请稍后重试」；**传输层失败**（无 HTTP 响应/IO 异常）才显示
  「网络错误，请稍后重试」——两类区分开，不再把 VALIDATION_ERROR 等误报成网络错误。

### 4.2 倒计时提示组件（登录/注册屏）

- `AuthMessage` 改造：错误文案 + **右侧纯数字倒计时【3】【2】【1】**（无省略号），
  每秒递减，3 秒倒计时归零时**整个错误提示框消失**。
- 新错误出现时重置倒计时（从 3 重新开始）。
- 实现：Compose `LaunchedEffect` + `delay(1000)` 驱动每秒递减；组件可单测。
- 范围：仅登录/注册屏（用户裁决，不做全 App 统一）。

## 5. 平台 backlog

### 5.1 bootstrap 429 重试（register/login）

- `test-platform/shanka/account.py` 的 register/login 遇 429：
  - 按响应 `Retry-After` 等待 + 重试，**限 3 次**；
  - 仍 429 则明确报错（含建议等待时长），不再静默 `session=None` 导致 live_flow 会话失败。
- 依据：429 是服务端在业务执行**之前**的明确拒绝（限流桶检查先于建用户/建会话），
  重试无重放副作用；与 FR-19「客户端不得自动重试 register/login 防网络重放」
  （防超时/5xx 未知结果重放）不冲突。

### 5.2 PLANNING 预算前提同步

- `test-platform/shanka/cost.py`：derive_budget 的 PLANNING 前提按 fixture 声明改为
  **3 规划组**（样书前 2 章 42.6k 字符 ÷ `planner_max_input_chars` 20k 向上取整），
  预算推导不再欠报；同步更新 test-platform/CLAUDE.md 前提声明句与
  `docs/superpowers/specs/2026-08-12-test-platform-design.md` 相关表述（按惯例追加勘误注）。

## 6. 契约同步（四处一致）

- **PRD**：新建 `docs/PRD/V2.4/prd_v2_4.md`（继承 V2.3），变更清单记录：
  email 登录键、注册三字段、username 展示名放宽+去唯一、滑动续期、EMAIL_TAKEN 新增、
  USERNAME_TAKEN 移除、INVALID_CREDENTIALS 文案、错误提示 UX（3 秒倒计时）、忘记密码文案。
- **database-design.md**：2.15 users 加 email 行（NOT NULL/UNIQUE/lowercase 规范化）、
  username 描述改展示名（去唯一、格式放宽）；2.16 auth_sessions 补充滑动续期语义；
  §7.1 追加 V2.4 落地记录。
- **structure-contract.md**：3.14 AuthUser username 描述改展示名；6.11 接口表
  （register 三字段/email 冲突 EMAIL_TAKEN/login email/滑动续期语义）；7 错误码表
  （EMAIL_TAKEN 新增、USERNAME_TAKEN 移除、INVALID_CREDENTIALS 文案）；1.6 登录桶键 email。
- **openapi.yaml**：AuthRegisterRequest（username/email/password 及各自约束）、
  AuthLoginRequest（email/password）、错误码响应与错误码表同步。
- 红线守卫：ORM↔database-design、schemas↔openapi↔structure-contract 每个提交内全绿。

## 7. 测试计划

- **后端**（main/tests）：注册三字段 + email 规范化 + EMAIL_TAKEN；登录 email +
  桶键 email + 文案「邮箱或密码错误」；滑动续期（过期线内会话续期/新鲜会话不续/
  revoked 不续/每天节流边界）；迁移测试改写（升级后 users 空 + email 列 + downgrade 拒绝）。
- **前端 JVM**：全量映射表逐码断言 + 未知码兜底 + 传输层失败文案；
  倒计时组件（3→2→1 递减、归零整个框消失、新错误重置）；
  register 三参数接线（含确认密码不一致不提交）；登录 email 传参。
- **前端 instrumented**：登录链路场景更新为 email 语义（T12/T13 现有用例相应调整）；
  真机验证倒计时行为（连跑 connectedDebugAndroidTest，复用 Windows adb server 通道）。
- **平台**：bootstrap 429 重试单测（stdlib unittest）；derive_budget PLANNING 3 组断言。
- **收尾**：后端四工具（pytest/ruff/format/mypy）+ 平台 82 + gradle 53 + assembleDebug +
  真机 connectedDebugAndroidTest 全绿。

## 8. 验收标准

- [x] AC-01 注册收 username+email+password；email lowercase 规范化 + 唯一；重复 email → 409 EMAIL_TAKEN。
- [x] AC-02 登录只用 email+password；错误密码/不存在邮箱 → 401「邮箱或密码错误」；限流桶按 email。
- [x] AC-03 主界面展示注册时的 username（「用户名，快来学习」）。
- [x] AC-04 滑动续期：活跃使用不会过期；连续 30 天不活跃过期；登出立即失效。
- [x] AC-05 错误提示显示 3 秒后整个框消失，右侧倒计时纯数字【3】【2】【1】每秒递减，新错误重置。
- [x] AC-06 前端错误映射覆盖后端全部错误码，未知码兜底「操作失败，请稍后重试」，
  传输层失败才显示「网络错误，请稍后重试」。
- [x] AC-07 忘记密码提示文案为「请手动联系开发者直接修改密码。」。
- [x] AC-08 注册确认密码两次一致才提交；密码提示「至少 8 位」。
- [x] AC-09 平台 bootstrap 429 按 Retry-After 重试 ≤3 次并明确报错；PLANNING 预算按 3 组推导。
- [x] AC-10 PRD V2.4 / database-design / structure-contract / openapi 四处一致 + 三面回归全绿。

## 9. 执行纪律（继承全局约束）

- 无破坏性 git（禁 push/PR/fork/force/reset --hard）。
- 凭据纪律：DEEPSEEK_API_KEY/token/密码不进日志、报告、命令参数；.env 权限 600 且被忽略。
- 不触碰 `docs/llm-account-long-run-v1/`、`docs/account-auth-test-platform-long-run-v1/`。
- 后端运行中（uvicorn 127.0.0.1:8000）——联调期间不要停。
