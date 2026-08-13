# Android 登录切换 P6 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Android 客户端完成账号登录切换：Login/Register 界面、Keystore 加密会话存储替代 SecureDeviceIdentityStore、网络层 Bearer、普通请求彻底移除 X-Device-ID、401 清会话回登录页。

**Architecture:** 4 任务：会话存储 → auth 网络层 → UI 与启动路由 → 收尾与构建验证。仓库为嵌套 git（`frontend-app/`，HEAD 2a9f6b7）；每任务在 frontend-app 内独立提交，外层仓库不参与（P1 先例）。

**Tech Stack:** Kotlin、Jetpack Compose（单 Activity）、HttpURLConnection（现有 BackendClient）、Android Keystore、JUnit4（现有 test/）。

## 全局约束

1. 工作目录 `/home/kbzz1/shanka_backend/frontend-app/Front`；git 操作在 `frontend-app` 仓库内（`git -C /home/kbzz1/shanka_backend/frontend-app add/commit`）；不提交外层仓库。
2. 验收（每任务）：`./gradlew test`（JVM tests）+ `./gradlew assembleDebug`（任务 1-3 各自至少 test 绿；assembleDebug 全绿在任务 4 总验——按仓库 AGENTS 规则）或按既有构建流程。
3. TDD：先写失败测试（JVM 可测层：存储接口、client 请求构造、401 处理逻辑），再实现。
4. 契约权威（DESIGN §4.2~4.4 / WORKER_PROMPT §5）：register/login 请求仅 `{username, password}`；成功响应 `{user{user_id,username,created_at}, access_token, token_type="Bearer", expires_at}`；普通请求 `Authorization: Bearer <token>`；register/login 不加 Bearer；普通请求彻底移除 X-Device-ID；服务端 401 清会话回登录页（不无限重试）；网络失败不误判退出；密码不持久化；无旧数据认领提示。
5. 敏感信息：密码/token 不进日志（client 日志沿用敏感路径脱敏——请求日志不记 body/Authorization）；测试用假凭据。
6. 不碰 docs/llm-account-long-run-v1/、docs/account-auth-test-platform-long-run-v1/（只读）；无破坏性 git。

## 现状基线（2026-08-14，frontend-app HEAD 2a9f6b7）

- `data/remote/RemoteFlashcards.kt`（540 行）：`BackendClient`（request/execute/executeMultipart，HttpURLConnection，X-Device-ID 注入在 :193/:219）、`SecureDeviceIdentityStore`（private class :96-131，SharedPreferences "remote_identity" + Keystore 别名 "shanka_device_identity_key"）、`RemoteFlashcardRepository`、`ApiResult`/`HttpResult` 密封类。
- `ui/AppViewModel.kt`（344 行）、`ui/Screens.kt`、`ui/navigation/`、`MainActivity.kt`（29 行）。
- 测试：`app/src/test/java/.../data/`（ReviewSchedulerTest/ImportParserTest——JVM 可跑）+ androidTest（instrumented，BackendClientInstrumentedTest）。
- 未跟踪：`frontend-app/CLAUDE.md` 符号链接（agent-md 维护创建，随本计划任务 1 一并提交入 frontend-app 仓库）。

## 任务

### Task 1: 会话安全存储（Keystore 加密 SessionStore 替代 SecureDeviceIdentityStore）

**Files:**
- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/data/session/SessionStore.kt`
- Delete: `RemoteFlashcards.kt` 内 `SecureDeviceIdentityStore`（:96-131，随任务 2/4 全部引用移除后删）
- Test: `Front/app/src/test/java/com/qiuzhao/flashcards/data/session/SessionStoreContractTest.kt`

**Interfaces:**
- Consumes: Android Keystore（既有别名迁移或新别名 "shanka_session_key"）。
- Produces（后续任务依赖，签名逐字）：
  - `interface SessionStore { fun save(token: String, user: SessionUser); fun load(): Session?; fun clear() }`
  - `data class SessionUser(val userId: String, val username: String, val createdAt: String)`
  - `data class Session(val token: String, val user: SessionUser)`
  - `class KeystoreSessionStore(context: Context) : SessionStore`——token 经 AES/GCM（Keystore 密钥）加密存 SharedPreferences（新文件名 "auth_session"）；密码绝不持久化（本接口无密码参数）。

- [ ] **Step 1: 写失败测试** SessionStoreContractTest（JVM 可测：接口契约 + 内存 fake 实现行为——save/load/clear 往返、覆盖旧会话、clear 后 load=null；Keystore 实现本身靠 androidTest 或手工构建验证，JVM 测试锁定契约）。
- [ ] **Step 2: 运行确认失败** `./gradlew test` → FAIL
- [ ] **Step 3: 实现** SessionStore.kt（接口 + Keystore 实现 + InMemorySessionStore 测试用 fake）
- [ ] **Step 4: 运行确认通过** `./gradlew test` 绿
- [ ] **Step 5: Commit**（frontend-app 内）
  ```bash
  git -C /home/kbzz1/shanka_backend/frontend-app add -A Front/app/src/main/java/com/qiuzhao/flashcards/data/session Front/app/src/test/java/com/qiuzhao/flashcards/data/session CLAUDE.md
  git -C /home/kbzz1/shanka_backend/frontend-app commit -m "feat(android-auth): P6-1 会话安全存储——Keystore 加密 SessionStore（替代 SecureDeviceIdentityStore）"
  ```

### Task 2: auth 网络层（Bearer 注入 + 四端点 + 401 语义）

**Files:**
- Modify: `Front/app/src/main/java/com/qiuzhao/flashcards/data/remote/RemoteFlashcards.kt`（BackendClient + Repository 加 auth 方法）
- Test: `Front/app/src/test/java/com/qiuzhao/flashcards/data/remote/AuthClientContractTest.kt`

**Interfaces:**
- Consumes: Task 1 `SessionStore`/`Session`。
- Produces：
  - `BackendClient`：构造注入 `sessionStore: SessionStore`；`execute` 普通请求移除 `setRequestProperty("X-Device-ID", ...)`，改为注入 `Authorization: Bearer <token>`（session 存在时；缺失 → 不带头，服务端 401 AUTH_REQUIRED）；新增
    `suspend fun register(username: String, password: String): HttpResult`（POST /auth/register，**不带头**）、
    `suspend fun login(username: String, password: String): HttpResult`（POST /auth/login，不带头）、
    `suspend fun logout(token: String): HttpResult`（POST /auth/logout，带头）、
    `suspend fun me(): HttpResult`（GET /auth/me，带头）。
  - `RemoteFlashcardRepository` 新增：`suspend fun register(username, password): ApiResult<Session>`（成功解析 `{user, access_token, token_type, expires_at}` → save 到 SessionStore）、`suspend fun login(...)` 同、`suspend fun logout(): ApiResult<Unit>`、`suspend fun refreshMe(): ApiResult<SessionUser>`。
  - 401 语义：`Failure(401, code=AUTH_REQUIRED|AUTH_INVALID)` 由调用方（Task 3 ViewModel）统一 `sessionStore.clear()` + 导航登录页；网络失败（IOException）≠ 401，不误判退出。

- [ ] **Step 1: 写失败测试** AuthClientContractTest（JVM：用内存 SessionStore + 可注入 URL/响应 fake——若 BackendClient 用 HttpURLConnection 难 mock，则把请求构造抽为可测函数：`buildHeaders(session: Session?) -> Map<String, String>` 与响应解析函数，JVM 直测：普通请求含 Bearer 无 X-Device-ID、register/login 无 Bearer、401 解析形状、登录成功解析 Session）。
- [ ] **Step 2-4: 红→实现→绿**（TDD）
- [ ] **Step 5: Commit**：`feat(android-auth): P6-2 auth 网络层——Bearer 注入/四端点/401 语义（普通请求移除 X-Device-ID）`

### Task 3: Login/Register UI + 启动路由

**Files:**
- Create: `Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthScreens.kt`（Login/Register Composable）
- Modify: `ui/AppViewModel.kt`（auth 状态：loading/loggedIn/loggedOut/error）、`MainActivity.kt`、`ui/navigation/`（启动路由）
- Test: `Front/app/src/test/java/com/qiuzhao/flashcards/ui/auth/AuthViewModelTest.kt`（JVM：ViewModel 逻辑——register/login 成功转 loggedIn、401 清会话转 loggedOut、网络失败不误判）

**行为契约**：
- 启动：SessionStore.load() 非空 → 调 /auth/me 验证 → 200 进主界面 / 401 清会话进登录页；无 session → 登录页。
- Login/Register 界面：username/password 输入（password 掩码、不持久化）；提交按钮 loading；401 INVALID_CREDENTIALS 文案「用户名或密码错误」、409 USERNAME_TAKEN 文案「用户名已被占用」；错误响应不含内部细节。
- 受保护请求 401（AUTH_REQUIRED/AUTH_INVALID）→ 清会话 + 回登录页（不无限重试）；INVALID_CREDENTIALS（仅 login 端点）不触发清会话。
- 网络失败（IOException/超时）→ 保留会话、提示网络错误、不退出。
- 无旧数据认领提示（无 legacy claim UI）。

- [ ] **Step 1: 写失败测试** AuthViewModelTest（JVM：注入 fake repository）
- [ ] **Step 2-4: 红→实现→绿**（TDD）
- [ ] **Step 5: Commit**：`feat(android-auth): P6-3 Login/Register 界面 + 启动路由（/auth/me 决定入口 + 401 清会话回登录页）`

### Task 4: 收尾——SecureDeviceIdentityStore 移除 + 构建验证

**Files:**
- Modify: `RemoteFlashcards.kt`（删 SecureDeviceIdentityStore 类与全部 X-Device-ID 残留）、其余 grep 命中处
- Test: 全量回归

**验收**：
- `grep -rn "X-Device-ID\|SecureDeviceIdentityStore\|shanka_device_identity" Front/app/src` 零命中（历史注释除外）。
- `./gradlew test` 全绿；`./gradlew assembleDebug` 成功（真实构建证据）。
- 敏感扫描：源码无真实凭据（测试用假用户名/密码）。
- 前端对接文档核对（docs/frontend/ 已 Bearer 化——若 frontend-app 内有 README/文档残留 device 头说明一并更新）。

- [ ] **Step 5: Commit**：`feat(android-auth): P6-4 收尾——SecureDeviceIdentityStore 移除 + X-Device-ID 零残留 + assembleDebug 验证`
