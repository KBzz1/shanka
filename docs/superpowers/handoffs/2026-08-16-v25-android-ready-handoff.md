# V2.5 非视觉车道 Android 就绪交接（NV_ANDROID_READY）

**日期**：2026-08-16
**来源**：V2.5 非视觉平台实施计划（`docs/superpowers/plans/2026-08-15-v2-5-nonvisual-platform.md`）Task 13 完成
**读者**：视觉车道 / 主执行 Agent / 集成验收

## Android 侧产物（嵌套仓库 worktree）

- 仓库根：`/home/kbzz1/shanka_backend/frontend-app`
- 分支：`codex/v25-nonvisual-data`（worktree `/home/kbzz1/shanka_backend/.claude/worktrees/v25-nonvisual-data`）
- 基线：`a5e1aae`（V2.4 main）；**就绪 tag：`v25-nv07-android-ready-20260815` → `fd9d25d`**（本地 tag，未推送远端）
- 提交链：`44aa373`（Task 1 domain/v25 typed bridge）→ `8cbad28` + `81e975a`（Task 12 remote data 层 + R1）→ `413f3d6`（用户自有签名/忽略 hunks 导入保留基线）→ `fd9d25d`（Task 13 Release 环境）
- 测试：全量 `./gradlew test` **143 tests / 0 failures**（controller 独立复跑确认）
- 签名 APK：`/home/kbzz1/shanka_backend/releases/app-release.apk`
  - 证书 DN：`CN=Shanka FlashCards, OU=Dev, O=Shanka, C=CN`（非 Debug 证书）
  - 版本 `2.5.0`（versionCode 仍为 1，按 brief）
  - SHA-256：`605a83b45339936f71a135c69f6a61059f7a7a430a5c37d9c80d2df252c9182a`
  - 原子产出：`scripts/build-release.sh`（构建→签名/包名/版本/SHA-256 校验→同目录 tmp+mv 原子替换；失败保留旧 APK）

## 视觉车道待办（cross-lane 开放项，需视觉侧处理或确认）

1. **frontendTestMode 运行时开关（P0，约束 13 冲突）**：V2.4 遗留的 `frontendTestMode`（AppViewModel/SettingsScreen，DataStore 持久化，喂 FrontendTestFixtures）**未按 BuildConfig.DEBUG 门禁，Release 同样可达**——与全局约束 13「Release 无 runtime test mode」相悖。该代码在 `ui/**`/`ui/AppViewModel.kt`（约束 10 禁改），属视觉车道。建议：读取处按 `BuildConfig.DEBUG` 门禁或彻底移除该开关。
2. **API_BASE_URL 双编码点**：`BuildConfig.API_BASE_URL`（build 期正式域名，Task 13）与 `data/remote/v25/V25Api.kt:153` 运行时硬编码 `https://shanka.kbzz1.top` 并存——建议 V25Api 接线 `BuildConfig.API_BASE_URL` 单源（已登记 Task 15 终审 fix）。
3. **version 类型**：后端 `Card.version`/`LearningProject.version` 为字符串（vN / ISO 时间戳），Task 1 域模型为 Int（Task 12 做最佳努力映射 vN→N、ISO→epochSeconds、其余 0；`updated_at`/`baseCardVersion` 为变更检测权威）。若视觉侧需要原始字符串，须经 domain/v25 修订并按 Architecture §8 通知。
4. **自由刷题随机序**：当前 seed=(user_id, deck_id) 跨会话/跨天恒定（契约无 seed 参数）；如需逐日变化可用 (user_id, deck_id, study_date)，同日会话稳定性不变。

## 接口摘要（视觉侧对接点）

- `domain/v25/V25Repository.kt`：46 方法全部由 `RemoteV25Repository` 实现；视觉代码不接触 DTO/HTTP/JSON。
- 统计：`/stats/dashboard` 无客户端参数（时区/周目标服务端派生）；周日期按 wire `timezone` 字段投影（R1 已修 UTC+ 错日缺陷）。
- 重写：两阶段 preview/apply/cancel 三端点（V2.4 单步 rewrite 已下线）。
- 删除：10 秒撤销批次，`undo_until` 服务端权威。
- `saveApiKey` 接收明文（上传必需），零日志承诺（KDoc 锁定）。
