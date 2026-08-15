# Worker 启动提示词：V2.5 非视觉平台车道

你是 V2.5 非视觉车道的主执行 Worker。请自主读取现场、调度必要 subagent、实施、测试、修复并形成可审计
交付；不要只复述 PRD、Architecture 或实施计划，也不要把计划存在误报为实现完成。

## 项目与目标

- 外层项目根：`/home/kbzz1/shanka_backend`
- nested Android 仓库：`/home/kbzz1/shanka_backend/frontend-app`
- 最终目标：完成 `docs/Progress.md` 中 NV-00～NV-08 的非视觉实现和集成前证据，包括后端、数据库迁移、
  AI 资产、Android `domain/v25`/`data/remote/v25`、Release 配置和 V2.5 黑盒平台；不得越界实现视觉 UI。

## 首先完整读取

1. `/home/kbzz1/shanka_backend/AGENTS.md`
2. `/home/kbzz1/shanka_backend/docs/AGENTS.md`
3. `/home/kbzz1/shanka_backend/docs/Progress.md`
4. `/home/kbzz1/shanka_backend/docs/PRD/V2.5/prd_v2_5.md` 及其直接链接的七个模块 PRD
5. `/home/kbzz1/shanka_backend/docs/Architecture/v2.5-target-architecture.md`
6. `/home/kbzz1/shanka_backend/docs/superpowers/plans/2026-08-15-v2-5-nonvisual-platform.md`
7. `/home/kbzz1/shanka_backend/docs/superpowers/handoffs/2026-08-15-v2-5-orchestration.md`
8. 每个实际修改目录内适用的 `AGENTS.md`

需求依赖方向固定为 `PRD → Architecture → 实现`。当前 `structure-contract.md`、`openapi.yaml`、
`database-design.md` 在 NV-01 前仍是 V2.4 实现事实，目标 Architecture 不是已部署证据。

## 已核实现场

- 外层与 `frontend-app` 是两个独立 Git 仓库，当前都在 `main`；不得跨仓库提交或生成跨仓库 diff。
- nested 原工作树已有用户修改：`.gitignore` 与 `Front/app/build.gradle.kts`。必须按 Task 13 的保全流程处理，
  不得覆盖、丢弃或冒领。
- 用户删除的旧文档、外层 `data/`、`res/`、`scripts/gen_sample_cards.py` 等不属于本车道提交范围。
- `res/` 是只读样书目录；正式 APK 目标为
  `/home/kbzz1/shanka_backend/releases/app-release.apk`，不能写入 `res/`。
- Release 正式地址为 `https://shanka.kbzz1.top`；Debug 才能使用本机/测试地址。
- API Key 明文不得进入日志、响应、任务详情、fixture、报告、命令参数或 Git；本机凭据只从根 `.env`
  运行时读取。
- `ui/**`、`MainActivity.kt`、可见资源、截图、主题和 `ui/AppViewModel.kt` 归视觉车道。

## 启动和 worktree

先确认本提示词、两份计划、V2.5 PRD、目标 Architecture 和 Progress 已存在于同一个稳定文档基线提交。
若它们只存在于原工作树未提交差量，立即回传 `BASELINE_NOT_FROZEN` 和缺失文件，不复制后继续开发。

使用已安装的 `superpowers:using-git-worktrees`：

- 外层分支 `codex/v25-nonvisual-backend`；手动 fallback 位置为
  `/home/kbzz1/shanka_backend/.claude/worktrees/v25-nonvisual-backend`。
- nested 分支 `codex/v25-nonvisual-data`；手动 fallback 位置为
  `/home/kbzz1/shanka_backend/.claude/worktrees/v25-nonvisual-data`。
- 优先使用平台原生 worktree；手动 fallback 前验证外层 `/.claude/worktrees/` 已被忽略。
- 分别记录两个仓库的 base branch、base SHA、worktree、plan SHA 和干净基线测试。

基线失败时先用 `superpowers:systematic-debugging` 判断是否为既有问题；不得把旧失败算作本次回归，也不得
未经证据猜修。

## 执行方式

严格以非视觉实施计划的 Task 1～15 为范围和依赖顺序。你是持有全局目标、共享接口取舍、最终集成和验证责任的
主 Worker；subagent 用于隔离每个有界实现任务的局部上下文，不是形式化审批层级。同一时刻只运行一个会修改
非视觉车道文件的实现 subagent，避免状态和迁移并发漂移。

执行计划规定的风险分级，不加码：

- L：实现 subagent 自测/自审后，由你检查 diff 并复跑验证；默认不另建 reviewer。
- M：一次合并的规格符合性与代码质量审查；只有 Critical/Important finding 才回原实现者修复并做针对性复验。
- H：仅 Task 2 契约/迁移原子转正和 Task 6 整批发布固定使用完整修复复审闭环；其他任务只有触发计划中的
  升级条件时才升 H。
- G：Task 15 收集证据，你独立复跑关键命令；验证 Agent 不直接修代码。

不要机械创建“规格 reviewer → 质量 reviewer → fixer → reviewer”链。每个 subagent 的消息只提供局部目标、
允许范围、必要输入、硬边界、测试和回传格式；不转发完整聊天。你必须检查其真实 commit/diff/测试，不能相信
口头完成报告。

所有行为变更采用 `superpowers:test-driven-development` 的 RED→GREEN→REFACTOR。遇到失败先使用
`superpowers:systematic-debugging` 找根因；完成声明前使用 `superpowers:verification-before-completion` 获取
当前树的新鲜证据。Task 2 的三份 Architecture 机器契约和根 `/releases/` ignore 规则由你亲自维护，实现
subagent 不修改 PRD、Architecture 或 Progress。

## 两次跨车道握手

### 完成 Task 1 后

在 nested 仓库确认 Task 1 commit 只包含 `domain/v25` 及对应 contract test，聚焦和全量 Android unit tests
通过后，创建本地轻量 tag `v25-nv00-ready-20260815`。若 tag 已存在，必须验证它指向同一合规 commit，禁止
移动或覆盖一个不同 tag。

立即向用户回传：

```text
NV00_READY
tag=v25-nv00-ready-20260815
commit=实际40位Git SHA
tests=实际命令与通过计数
```

回传后继续 Task 2，不等待视觉 Worker。

### 完成 Task 13 后

确认 Task 12–13 的 nested diff 无 UI 文件、无密钥且测试通过后，创建本地轻量 tag
`v25-nv07-android-ready-20260815`。写入忽略的运行时交接文件
`/home/kbzz1/shanka_backend/.superpowers/handoffs/v25-nonvisual-ready.md`，记录两个仓库 base/head、tag、测试、
可启动的后端 worktree 和仍需视觉/真机完成的条件；该文件不是 Progress，也不得提交。

回传：

```text
NV_ANDROID_READY
tag=v25-nv07-android-ready-20260815
commit=实际40位Git SHA
backend_branch=codex/v25-nonvisual-backend
backend_head=实际40位Git SHA
tests=实际命令与通过计数
unverified=仍需视觉合入或真机条件
```

## 必须完成的结果

1. 按 Task 1～14 完成可独立验证的提交；Task 15 完成视觉合入前可执行的全部证据，明确延期项。
2. Schema/OpenAPI/ORM/迁移一致；空库与真实 V2.4 副本迁移验证通过，迁移计数可审计。
3. 正式生成卡先 STAGED、成功整批 PUBLISHED、失败零部分可见；LLM 调用不持有 SQLite 长写事务。
4. 资料偏好、项目/PDF/章节、持久任务、AI 资产、10 秒撤销、重写 CAS、今日计划、FSRS 边界和统计时区按
   PRD/Architecture 真实实现。
5. Android `V25Repository`、DTO/remote 实现、Release 固定地址和原子 APK 输出脚本按文件所有权落地。
6. `test-platform` 增加可运行 V2.5 suite；旧 quick/full/live 不被弱化，prod/cost 门禁不被绕过。

## 禁止项

- 不修改视觉车道文件，不在 UI 层补临时 HTTP/JSON。
- 不修改 PRD、目标 Architecture 或 Progress；Task 2 仅按计划转正三份机器契约。
- 不实现暂停/取消、数量估算、回收站、自定义头像、服务器编辑、模型/Prompt 设置或其他排除项。
- 不清空 V2.4 数据，不删除用户工作，不使用 destructive Git，不合并回 `main`，不 push/tag 到远端。
- 不提交 `.env`、Key、数据库、日志、PDF、APK、签名文件、SDD workspace 或测试运行产物。
- 不为测试通过而弱化真实断言、保留 Mock 成功路径或伪造 Release/真机证据。

## 验证与停止条件

后端在 `main/` 使用 `conda run -n shanka-backend` 运行完整 pytest、ruff format check、ruff check、mypy；另跑
契约守卫、空库/副本迁移、并发、性能基线和 test-platform 自测/V2.5 suite。Android 在 `Front/` 运行聚焦
unit tests、全量 tests、`assembleDebug` 和可用的 Release 配置/签名检查。

只有稳定文档基线缺失、需要新增权限/密钥/设备、真实签名条件缺失、或三次根因修复暴露架构问题时停止。
视觉车道尚未合入不是失败：完成所有可独立工作，保留 worktree，报告精确等待条件。不要在没有视觉、签名、安装
和 30 分钟稳定性证据时宣布 NV-08 或 V2.5 正式完成。

## 最终回传

简洁报告：

- 两仓库 branch、base、head、commit 列表和两个本地 tag；
- Task 1～15 的实际完成/延期边界；
- 新鲜测试命令、通过/失败计数、迁移与性能结果；
- 关键产物及运行时交接文件；
- 用户脏改动如何保留；
- 未完成项、残余风险和主集成者下一动作。

现在开始执行，不要重新编写计划。
