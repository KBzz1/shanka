# 前后端联调工作区设计（2026-08-12）

## 1. 目标与范围

本机建立前后端联调工作区:前端代码进入本机、与后端仓库共处、可改可推。**本次只做环境就位,不承担具体联调任务**(联调任务在代码就位后另行规划)。

范围:

- 克隆前端仓库到本机指定目录;
- 后端仓库 .gitignore 排除前端代码;
- 联调指引文档(启动后端、编译前端、模拟器访问);
- 设计记录(本文)。

不在本次范围:后端代码改动(无 CORS/契约/openapi 变更)、Android 工具链安装、具体联调任务。

## 2. 背景事实

- 前端 = `Front` Android Jetpack Compose 应用(包名 `com.qiuzhao.flashcards`),远程仓库 `https://github.com/JIANGYOU3/Shanka`(main,2026-08-12 提交 `544891b`)。
- 本机 WSL2:无 Android SDK / adb,有 Java 21;后端运行方式 `scripts/run.sh`(8000/8001)。
- 前端侧联调状态文档:`frontend-app/docs/前端对接与联调交接.md`(2026-08-12 更新),自动化测试 13/13 通过,遗留一项待后端核对(见 §5)。

## 3. 决策记录

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 前端代码位置 | 后端仓库根下 `frontend-app/`(用户选定) | 与后端代码/文档共处一处,便于本机联调 |
| git 纳入 | 根 .gitignore 追加 `/frontend-app/`(用户确认) | 前端独立 git 历史;后端 pre-commit/ruff/mypy 与契约红线均围绕 Python,避免污染;git 视角前端目录不存在,故不进 Progress.md 文档清单 |
| 代码获取 | `git clone`(用户提供远程 URL) | 保留前端完整 git 历史,修改后可直接推送回 GitHub |
| 联调指引位置 | `docs/frontend/local-dev.md`(后端仓库) | `frontend-app/` 内放文件会污染前端仓库(untracked);`docs/frontend/` 已是前端对接文档目录 |
| WSL2 工具链 | 不安装 Android SDK | 编译走 Windows 侧 Android Studio(`\\wsl$` 路径);YAGNI,模拟器本就不能在 WSL2 内跑 |

## 4. 产物

- `/home/kbzz1/shanka_backend/frontend-app/` — 前端仓库克隆(独立 .git,`Front/` 工程 + `docs/` + `AGENTS.md`)。
- `.gitignore` — 追加 `# 前端联调工作区` + `/frontend-app/`。
- `docs/frontend/local-dev.md` — 联调指引:代码位置、后端启动、Windows 侧编译路径、`10.0.2.2:8000` 模拟器地址、请求头速查、协作流。

## 5. 联调任务候选(环境就位后另行规划,不属本次范围)

1. **样卡 400 核对**:前端 2026-08-12 00:39-00:40(Asia/Shanghai)记录 `POST /samples` 连续 `400 VALIDATION_ERROR`;X-Request-ID `75301a4cdc2147f5864512c8411f71b9` / `2a5d4b147d42422aa16e7d8890fc7be4` / `fcf6c45a85cc47c58094bf3db644d6fe`,可查 `main/data/logs/app.log` 定位校验失败字段。
2. 前端「当前待核对」「仍未实现的 UI / 能力」中其余项(见前端仓库交接文档)。

## 6. 验证

- 克隆后 `git -C frontend-app log --oneline` 正常(2 commits),working tree 干净。
- 根仓库 `git status` 不显示 `frontend-app/`(已忽略)。
