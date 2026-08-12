# 前后端本地联调工作区（2026-08-12 建立）

本文记录前端代码在**本机**的存放位置与联调方法；接口与行为契约仍以 `backend-integration.md`、`../Architecture/openapi.yaml`、`../Architecture/structure-contract.md` 为准。

## 1. 代码位置

| 项 | 位置 |
| --- | --- |
| 前端代码（本机） | `/home/kbzz1/shanka_backend/frontend-app/`（仓库根下，**被后端 git 忽略**，有独立 git 历史） |
| 前端远程仓库 | `https://github.com/JIANGYOU3/Shanka`（main 分支） |
| 前端工程目录 | `frontend-app/Front/`（Android Jetpack Compose，包名 `com.qiuzhao.flashcards`，自带 gradle wrapper） |

- 前端代码属于独立仓库：修改后推送回 GitHub，前端开发者自行拉取合并；后端仓库的 `frontend-app/` 仅是本机工作副本。
- 前端侧自己的联调状态与待核对项见 `frontend-app/docs/前端对接与联调交接.md`。

## 2. 联调环境

- **后端**：WSL2 内 `/home/kbzz1/shanka_backend/scripts/run.sh` 启动，端口 8000（被其他程序占用自动换 8001）。探活：`GET /healthz`。
- **前端编译/运行**：WSL2 内**没有 Android SDK**，编译在 Windows 侧 Android Studio 完成——打开 `\\wsl$\<发行版>\home\kbzz1\shanka_backend\frontend-app\Front`。
- **模拟器访问后端**：`http://10.0.2.2:8000`（Android 模拟器访问 WSL2 宿主；debug 专用）。真机/生产走 `https://shanka.kbzz1.top`。
- 当前路径**无 `/v1` 前缀**（实现状态，契约规划前缀对齐为后续任务，落地前联调用无前缀路径）。

## 3. 请求头速查

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `X-Device-ID` | 所有接口（除探活/metrics） | UUID v4，匿名设备 ID，等同密码，勿写日志 |
| `Idempotency-Key` | 所有写操作 | UUID v4；新操作新键、重试同键；`POST /samples` 豁免 |

## 4. 当前待办与已知问题

- **样卡 400 待核对**（前端 2026-08-12 记录）：`POST /samples` 连续返回 `400 VALIDATION_ERROR`，前端称请求字段齐全。后端按 X-Request-ID 核对：`75301a4cdc2147f5864512c8411f71b9`、`2a5d4b147d42422aa16e7d8890fc7be4`、`fcf6c45a85cc47c58094bf3db644d6fe`（对应日志 `main/data/logs/app.log`）。
- 更多前端侧遗留项见 `frontend-app/docs/前端对接与联调交接.md` 的「当前待后端核对」「仍未实现的 UI / 能力」。

## 5. 协作流

1. 前端代码修改 → 在 `frontend-app/`（前端仓库）内提交并推送 `git push origin main`。
2. 前端开发者 `git pull` 获取修改。
3. 后端仓库不含前端代码（`.gitignore` 排除 `frontend-app/`），前后端各自的 git 历史互不干扰。

## 6. 首次联调检查清单

- [ ] 后端启动：`./scripts/run.sh` → `curl localhost:8000/healthz` 返回 200（本机链路已实测；端口被占自动换 8001，模拟器地址同步改为 `http://10.0.2.2:8001`）
- [ ] Windows 侧：Android Studio 打开 `\\wsl$\<发行版>\home\kbzz1\shanka_backend\frontend-app\Front`（首次打开会下载 Gradle 依赖，较慢属正常）
- [ ] 运行目标：启动 AVD 模拟器（或真机 USB 调试），App 的 debug 后端地址指向 `http://10.0.2.2:8000`
- [ ] 链路实测：模拟器内访问 `GET http://10.0.2.2:8000/healthz` 返回 200
- [ ] 请求头就绪：`X-Device-ID`（全部接口）+ `Idempotency-Key`（写操作，`POST /samples` 豁免）

## 7. 联调数据说明

- 联调产生的业务数据（牌组/卡片/PDF/任务/统计）与加密 API Key 全部落在后端 `main/shanka.db`（SQLite，git 忽略）。
- 需要干净起点时：停止后端 → 备份该文件（如 `cp main/shanka.db main/shanka.db.bak`）→ 删除原文件 → 重启后端（空库自动迁移建表）；删除后设备需重新 `PUT /api-key` 保存密钥。
- 设备 ID 即数据主体：模拟器/真机每次使用的 `X-Device-ID` 决定看到哪份数据，联调时建议固定一个测试 UUID 便于对照日志。
