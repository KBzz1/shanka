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
- **前端编译/运行**：WSL2 内**没有 Android SDK**，编译在 Windows 侧 Android Studio 完成——打开 `\\wsl$\<发行版>\home\kbzz1\shanka_backend\frontend\Front`。
- **模拟器访问后端**：`http://10.0.2.2:8000`（Android 模拟器访问 WSL2 宿主；debug 专用）。真机/生产走 `https://shanka.kbzz1.top`。
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

## 8. 真机（USB）联调（2026-08-12 实测）

手机 USB 直插 Windows 侧，`adb devices` 可见后即可联调。两种模式：

| 模式 | 地址 | 适用 |
| --- | --- | --- |
| 公网（零改动） | App 真机默认 `https://shanka.kbzz1.top`（`RemoteFlashcards.kt` 的 `defaultBaseUrl()`：debug 非模拟器即生产地址） | 真机验收、随时可用；实测 healthz 200（含隧道耗时 ~0.8s） |
| 本地 USB（快速迭代） | Windows 侧 `adb reverse tcp:8000 tcp:8000` 后，手机访问 `http://localhost:8000`；需将 `defaultBaseUrl()` 的 debug 真机分支改为 `http://localhost:8000` | 高频联调反馈，不走公网 |

- 验证命令：手机浏览器打开 `https://shanka.kbzz1.top/healthz` 或（reverse 后）`http://localhost:8000/healthz`，返回 `{"status":"ok"}` 即链路通；后端 `main/data/logs/app.log` 有对应 request complete 记录可核对。
- 前端代码改动在 `frontend/` 下提交到统一仓库 `main`，后端与 Release 构建从同一提交读取。

## 9. 自动化测试平台

- 位置：`test-platform/`（独立顶层目录，零依赖纯 stdlib，不依赖 main 的 conda 环境）。
- 常用命令：
  - `./test-platform/runner/run.sh --suite quick` — 无 Key 冒烟（后端需运行中）
  - `./test-platform/runner/run.sh --suite live [--confirm-cost]` — 完整制卡流程（真实生成，消耗 DeepSeek 余额）
  - `./test-platform/device/build/build_apk.sh` — WSL2 编译前端 debug APK
  - `./test-platform/device/install/install.sh` — 安装 APK 到已连真机
- 分层/场景地图/日志规范/新增场景指引见 `test-platform/AGENTS.md`；设计见 `docs/superpowers/specs/2026-08-12-test-platform-design.md`。
