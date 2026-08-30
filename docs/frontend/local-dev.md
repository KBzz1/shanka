# 前后端统一仓库联调（2026-08-29 收敛）

本文记录前端代码在**本机**的存放位置与联调方法；接口与行为契约仍以 `backend-integration.md`、`../Architecture/openapi.yaml`、`../Architecture/structure-contract.md` 为准。

## 1. 代码位置

| 项 | 位置 |
| --- | --- |
| 前端代码（统一仓库） | `/home/kbzz1/shanka_backend/frontend/`（后端仓库 `main` 的 tracked subtree） |
| 主要分支 | 后端与前端统一使用本仓库 `main` |
| 前端工程目录 | `frontend/Front/`（Android Jetpack Compose，包名 `com.qiuzhao.flashcards`，自带 gradle wrapper） |

- 前端历史通过 subtree 保留在统一仓库中；前端与后端修改均在本仓库 `main` 上评审、提交和发布。
- 前端侧联调状态与待核对项见 `frontend/docs/前端对接与联调交接.md`。

## 2. 联调环境

- **后端**：WSL2 内 `/home/kbzz1/shanka_backend/scripts/run.sh` 启动，端口 8000（被其他程序占用自动换 8001）。探活：`GET /healthz`。
- **前端编译**：WSL2 内可直编（SDK 位于 `~/android-sdk`，工程自带 gradle wrapper）：`env -C frontend/Front ./gradlew assembleDebug`；也可在 Windows 侧 Android Studio 打开 `\\wsl$\<发行版>\home\kbzz1\shanka_backend\frontend\Front`。
- **Debug 包名**：`com.qiuzhao.flashcards.debug`（`applicationIdSuffix = ".debug"`，本机 debug keystore 自动签名），与正式包 `com.qiuzhao.flashcards` **并存互不影响**；测试包为 `com.qiuzhao.flashcards.debug.test`。
- 当前路径**无 `/v1` 前缀**（实现状态，契约规划前缀对齐为后续任务，落地前联调用无前缀路径）。

## 3. 请求头速查

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <token>` | 所有业务接口（除 register/login、探活/metrics） | 注册/登录获得，等同密码，勿写日志 |
| `Idempotency-Key` | 所有写操作 | UUID v4；新操作新键、重试同键；`POST /samples` 豁免 |

## 4. 当前待办与已知问题

- **样卡 400 待核对**（前端 2026-08-12 记录）：`POST /samples` 连续返回 `400 VALIDATION_ERROR`，前端称请求字段齐全。后端按 X-Request-ID 核对：`75301a4cdc2147f5864512c8411f71b9`、`2a5d4b147d42422aa16e7d8890fc7be4`、`fcf6c45a85cc47c58094bf3db644d6fe`（对应日志 `main/data/logs/app.log`）。
- 更多前端侧遗留项见 `frontend/docs/前端对接与联调交接.md` 的「当前待后端核对」「仍未实现的 UI / 能力」。

## 5. 协作流

1. 前端代码修改 → 在 `frontend/` 下修改，在统一仓库根目录提交（例如 `git add frontend/`）。
2. 后端代码修改 → 在 `main/` 下修改；需要跨层变更时在同一提交或同一评审链中同步契约。
3. 统一仓库的 `main` 是唯一发布分支；不再向独立前端仓库推送或从独立 worktree 合并。

## 6. 首次联调检查清单

- [ ] 后端启动：`./scripts/run.sh` → `curl localhost:8000/healthz` 返回 200（本机链路已实测；端口被占自动换 8001，模拟器地址同步改为 `http://10.0.2.2:8001`）
- [ ] Windows 侧：Android Studio 打开 `\\wsl$\<发行版>\home\kbzz1\shanka_backend\frontend\Front`（首次打开会下载 Gradle 依赖，较慢属正常）
- [ ] 运行目标：启动 AVD 模拟器（或真机 USB 调试），App 的 debug 后端地址指向 `http://10.0.2.2:8000`
- [ ] 链路实测：模拟器内访问 `GET http://10.0.2.2:8000/healthz` 返回 200
- [ ] 请求头就绪：先 `POST /auth/register`（或 `/auth/login`）拿 token，业务请求带 `Authorization: Bearer`；写操作带 `Idempotency-Key`（`POST /samples` 豁免）

## 7. 联调数据说明

- 联调产生的业务数据（牌组/卡片/PDF/任务/统计）与加密 API Key 全部落在后端 `main/shanka.db`（SQLite，git 忽略）。
- 需要干净起点时：停止后端 → 备份该文件（如 `cp main/shanka.db main/shanka.db.bak`）→ 删除原文件 → 重启后端（空库自动迁移建表）；删除后需重新注册/登录并重新 `PUT /api-key` 保存密钥。
- 账号即数据主体：登录的 `user_id` 决定看到哪份数据，联调时建议固定一个测试用户名便于对照日志。

## 8. Debug 后端地址与真机（USB）联调

统一网络栈（Retrofit/OkHttp，`data/remote/http/NetworkStack.kt` 的 `EndpointAuthority`）下，Debug 构建地址由 Gradle property 控制，Release 编译期固定、不可覆盖：

| 场景 | Debug App 使用的地址 | 说明 |
| --- | --- | --- |
| 模拟器（默认） | `http://10.0.2.2:8000` | 不传 property 时的默认值（模拟器访问 WSL 宿主） |
| USB 真机 + 本地后端（reverse） | `http://127.0.0.1:<port>` | 构建传 `-PshankaDebugApiBaseUrl=http://127.0.0.1:<port>`，配合 Windows 侧 `adb reverse tcp:<port> tcp:<port>`（手机 127.0.0.1 → 宿主端口；后端在 WSL 监听同端口即可）。注意：WSL 内另一个 adb server 曾与本机 Windows adb 争用 USB transport，导致 reverse 上游停摆（手机侧 connect 超时而非拒绝）——设备操作统一走 Windows adb，勿混用 WSL adb |
| USB/Wi-Fi 真机 + 本地后端（LAN 直连） | `http://<宿主 LAN IP>:<port>` | Windows 防火墙默认拦入站：需管理员放行（`netsh advfirewall firewall add rule name="Shanka dev <port>" dir=in action=allow protocol=TCP localport=<port> profile=private`；WSL mirrored 模式下如仍不通，另设 `Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow`）。后端需绑 `0.0.0.0`；明文 HTTP 由 debug-only manifest 的 `usesCleartextTraffic` 覆盖（Release 不合并） |
| 正式 Release | `https://shanka.kbzz1.top` | 编译期固定，Debug property 对 Release 无效 |

- property 只接受不含引号/空白/换行的合法 `http(s)://` URL，非法值在 Gradle 配置期直接报错（防止破坏 BuildConfig 字面量）；不提供 UI 内服务器切换。
- WSL 直调 Windows `adb.exe` 若出现 `UtilBindVsockAnyPort: socket failed 1`，改在 Windows PowerShell 执行 ADB 命令（`$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe`）。
- 真机联调装 **Debug 包**（`com.qiuzhao.flashcards.debug`）：与手机上的正式 App 并存，数据、会话与数据库完全隔离；不要卸载或覆盖正式包做联调。
- 断网/恢复演练用增删 `adb reverse` 精确模拟（LAN 直连模式下用启停隔离后端等效模拟；均不切手机飞行模式/Wi-Fi）。
- 验证链路：reverse 建立后，Debug App 登录或手机浏览器访问 `http://127.0.0.1:<port>/healthz` 返回 200 即通。
- 排障经验（真机验收实测）：`BuildConfig.API_BASE_URL` 是 `const val`，会在测试 APK 编译期内联——测试/probe 打印的地址不等于设备上实际安装 App 的运行时值。probe 与 App 端口不一致时，先 `pm path` 拉出实际安装的主 APK 与测试 APK，再查 dex 里烘焙的常量，不要只信测试打印值。
- MIUI 真机 instrumentation 排障：`toybox nc` 建连但零字节不能证明 reverse 中继损坏（结论不确定），用 App 自有 OkHttp 栈或浏览器真实 HTTP 判定；启动后约 2.5 秒再由宿主 `am start -W` 拉起真实 Activity（过早会与 instrumentation 的进程重启竞争）；Compose 持续重绘时 `startActivitySync` 的 idle 检测不会收敛，应改用 application-wide RESUMED latch 等前台。
- MIUI 还会无条件 abort instrumentation 期间 App 自身 UID 的 Activity 启动（`ActivityTaskManager: Abort background activity starts`，result code 102，即使 App 已有前台 Activity 也拦），Compose 测试规则的 `ActivityScenario` 因此永久挂起。androidTest 用 `RequiresOwnActivityLaunch` 金丝雀规则先探测该能力、被拦时 assume-skip；全量无 stage 运行由 `DeviceAcceptanceFlowTest#probe`（只读 healthz 探针）提供实际执行的测试，宿主编排改为 instrument 启动后约 14 秒（首个金丝雀超时 skip、probe 开始等待期间）单次 `am start` 前台化。

## 9. 自动化测试平台

- 位置：`test-platform/`（独立顶层目录，零依赖纯 stdlib，不依赖 main 的 conda 环境）。
- 常用命令：
  - `./test-platform/runner/run.sh --suite quick` — 无 Key 冒烟（后端需运行中）
  - `./test-platform/runner/run.sh --suite live [--confirm-cost]` — 完整制卡流程（真实生成，消耗 DeepSeek 余额）
  - `./test-platform/device/build/build_apk.sh` — WSL2 编译前端 debug APK（统一仓库路径 `frontend/Front`；装真机用 `-PshankaDebugApiBaseUrl` 时需自行调 gradle，见第 8 节）
  - `./test-platform/device/install/install.sh` — 安装 debug APK 到已连真机（默认装 `com.qiuzhao.flashcards.debug`）
- 分层/场景地图/日志规范/新增场景指引见 `test-platform/AGENTS.md`；设计见 `docs/superpowers/specs/2026-08-12-test-platform-design.md`。
