# V2.5 双车道 Worker 编排

本文只定义启动顺序、跨车道握手和最终集成责任，不建立第三份进度表。正式状态仍以
`docs/Progress.md` 为准。

## 1. 角色

| 角色 | 工作范围 | 启动提示词 |
| --- | --- | --- |
| 非视觉主 Worker | 后端、数据库、AI 资产、Android domain/data、Release 配置、黑盒平台 | `2026-08-15-v2-5-nonvisual-worker-prompt.md` |
| 视觉主 Worker | Android Compose 页面、交互、导航、可访问性、截图和视觉验收 | `2026-08-15-v2-5-visual-worker-prompt.md` |
| 主集成者 | 冻结文档基线、核验两边证据、合并、真机与正式 Release 总验、更新 Progress | 当前主任务，不另建 Worker |

## 2. 启动前置

主集成者先建立一个只包含已确认 V2.5 PRD、Architecture、Progress、两份实施计划和本组启动提示词的
稳定文档基线提交。不得顺带提交用户删除项、`data/`、`res/`、`scripts/gen_sample_cards.py`，或 nested
仓库的 `.gitignore`、`Front/app/build.gradle.kts` 脏改动。

没有稳定基线时两个 Worker 都不得开始实现；新 worktree 无法可靠继承未提交文档。

## 3. 启动顺序

1. 先启动非视觉 Worker。
2. 非视觉 Worker 完成 Task 1、验证 Android contract test 后，在 nested 仓库创建本地轻量 tag
   `v25-nv00-ready-20260815`，并回传 `NV00_READY`、commit 和测试结果；随后继续执行，不等待视觉 Worker。
3. 收到 `NV00_READY` 后启动视觉 Worker。视觉 Worker先完成 V-01，再核验并合入该 tag；不得复制或自行改写
   `domain/v25`。
4. 两个 Worker 分别在自己的 worktree 推进。非视觉 Worker 同一时刻只运行一个实现 subagent；视觉 Worker
   只在文件范围完全独立时按需使用 subagent。
5. 非视觉 Worker 完成 Task 12–13 后，在 nested 仓库创建本地轻量 tag
   `v25-nv07-android-ready-20260815`，供视觉 Worker 合入真实 domain/data/Release 配置并执行 V-08。
6. 任一依赖 tag 尚未存在时，消费方保留 worktree 并报告等待条件，不自行搭临时 HTTP、复制 DTO 或伪造数据。

## 4. 文件和 Git 边界

- Backend worktree：分支 `codex/v25-nonvisual-backend`。
- Android data worktree：分支 `codex/v25-nonvisual-data`。
- Android visual worktree：分支 `codex/v25-visual`。
- nested `.gitignore` 与 `Front/app/build.gradle.kts` 现有差量属于用户；非视觉 Worker按实施计划 Task 13
  的保全流程导入，视觉 Worker不得编辑或提交这些行。
- 视觉 Worker仅通过已验证 tag 合入 `domain/v25` 与 `data/remote/v25`；不 cherry-pick 未审查的 branch HEAD。
- 两个执行 Worker都不修改 PRD、目标 Architecture 或 Progress，也不合并回 `main`。

## 5. 两次握手

### NV-00 typed bridge

非视觉 Worker回传：

```text
NV00_READY
tag=v25-nv00-ready-20260815
commit=实际40位Git SHA
tests=实际命令与通过计数
```

视觉 Worker合入前确认该 tag 相对共同 nested 基线只修改：

- `Front/app/src/main/java/com/qiuzhao/flashcards/domain/v25/**`
- `Front/app/src/test/java/com/qiuzhao/flashcards/domain/v25/**`

### NV-07 Android data

非视觉 Worker回传：

```text
NV_ANDROID_READY
tag=v25-nv07-android-ready-20260815
commit=实际40位Git SHA
backend_branch=codex/v25-nonvisual-backend
backend_head=实际40位Git SHA
tests=实际命令与通过计数
unverified=仍需视觉合入或真机条件
```

视觉 Worker核验 tag 范围后合入，并执行 V-08 真实接口与视觉验收。若 tag 包含 UI 文件，拒绝合入并报告越界。

## 6. 最终汇合

两个 Worker都回传 ready 后，由主集成者：

1. 检查两外层/嵌套仓库的基线、commit range、用户脏改动和测试原文；
2. 核验视觉最终分支已经包含两个非视觉 tag；
3. 选择并执行合并策略，不由 Worker自行合并主分支；
4. 运行后端四工具、迁移、黑盒 V2.5 套件、Android tests、Release 签名/版本/hash、安装升级和 30 分钟稳定性；
5. 证据成立后才更新 `docs/Progress.md`，否则保留 TODO/DOING 并记录真实缺口。
