# Shanka 前端收尾修复计划（可靠性 + 数据真实）

日期：2026-08-28
状态：已确认范围，执行中。

## 摘要

按已确认范围执行：只改前端，优先解决重复写入、复习失败丢卡，以及页面伪造数据；缺失服务端指标保留现有版式并显示 `—`。生产 OpenAPI 目前有批量导卡与全局看板，但未发布项目统计接口，因此不接入本地旧文档里的项目统计能力。[生产 OpenAPI](https://shanka.kbzz1.top/openapi.json)

## 关键改动

- 为 JSON 写请求和 PDF 上传增加"调用方提供的幂等键"能力；一次用户操作生成固定 UUID，网络失败后的重试复用同一键。
- 将导卡统一为现有的原子批量导入接口：
  - 新卡组：固定幂等键创建卡组，再用另一固定幂等键批量导卡；
  - 任一步失败时保留草稿与已创建卡组 ID，重试只重放失败步骤，绝不再创建第二个卡组；
  - 已有卡组、手动加卡、文本导入和 PDF 流程中的文本导入都走同一提交协调器；
  - 提交期间禁用主按钮，成功后才跳转或清空草稿。
- 修复复习提交状态：
  - 评分后卡片、计数和进度只在服务端成功后更新；
  - 提交中禁用全部评分操作；
  - 失败时保留当前卡片并提供重试，复用同一 `client_event_id` 和幂等键，避免"服务端已写入但客户端未收到响应"造成重复复习。
- 修复 PDF 入口：
  - 改为一次选择一份 PDF；重新选择只替换尚未提交的本地文件；
  - 未选择文件时明确提示，上传中禁止重复启动；
  - 失败重试复用同一次上传的幂等键，避免重复创建项目。
- 清理页面伪造数据：
  - 首页接入真实昵称、今日计划和连续学习天数；无卡组时显示真实空状态，不再展示"用户名""计算机网络"等占位内容。
  - 卡组页仅展示可证明真实的总掌握数/卡片数；今日复习、学习时长、题型分布、周趋势等无接口指标显示 `—`。
  - 项目页仅从所属卡组聚合总卡片与掌握进度；今日项目数据、学习时长、分布与项目连续天数显示 `—`，移除 `12min`、`2.4h` 和固定分布。
  - 小于 1000 的真实掌握数直接显示整数，不再显示误导性的 `0.042k`。
- 本轮不重做已对齐的登录、项目/PDF、智能制卡流程；不清理旧 Room/排程代码，避免引入数据迁移风险。

## 接口与状态约束

- 扩展前端传输层，允许 `request`、`upload` 使用显式幂等键；默认调用仍保持现有自动生成行为。
- 增加前端内部的 `ImportAttempt`、`ReviewAttempt`、`PdfUploadAttempt` 状态，保存本次操作的 UUID、目标和草稿；页面旋转期间保留，进程完全终止后不自动重放，而是先刷新服务端状态。
- 增加 V2.5 批量导卡映射，解析现有 `ImportResponse`；导入页不再逐张 POST。
- 不新增或修改后端 API、数据库、OpenAPI 与部署配置。

## 生产契约事实（2026-08-28 核验）

- `POST /decks/{deck_id}/cards/import`，请求体 `{"cards": [{"front","back"}...]}`，响应 `ImportResponse = {"results": [{"index","status"(CREATED|FAILED),"card_id"?,"error"?}]}`，201。
- `POST /review-events`，请求体必填 `card_id`、`rating`、`client_event_id`；响应 `{review_state: ReviewState, study_date}`。
- `GET /stats/dashboard` → StatsDashboard：`period/timezone/weekly_activity/weekly_total/weekly_completed_count/week_change_rate/weekly_goal/weekly_goal_progress/recall_accuracy/first_answer_accuracy/retention_rate/streak_days/mastered_card_count/updated_at/has_data`。
- `GET /study/today` → TodayStudyPlan：`timezone/study_date/current_project?/daily_goal/today_completed_count/due_count/main_plan_remaining/backlog_count/cards[]`。
- `POST /projects`（multipart，字段 `file` + 可选 `name`）→ LearningProject。
- 无项目统计接口。

## 验证

- JVM/契约测试：显式幂等键、批量导卡序列化、复习 `client_event_id` 重放、失败后不会重复创建卡组或卡片。
- Compose 测试：导卡/上传按钮防连点；复习失败后卡片仍在、成功后才推进；无数据指标统一显示 `—`。
- 回归测试：首页真实今日目标与昵称、项目/卡组无伪造数值、PDF 未选文件和上传失败路径。
- 执行 `:app:testDebugUnitTest`、`:app:assembleDebug`，再由用户在真机验证导卡、断网重试、复习失败与首页/统计展示。
- 保留当前工作区已有未提交 UI 改动；仅本地提交，不推送 GitHub。

## 已锁定的默认

- 范围为"可靠性 + 数据真实"，不扩展后端指标。
- 服务端未提供的数据保留视觉位置并显示 `—`，不隐藏模块、不借用无关字段填充。

## Global Constraints

1. 只改前端仓库 `Front/`；不新增或修改后端 API、数据库、OpenAPI 与部署配置。
2. 保留工作区已有未提交改动（`AuthScreen.kt`、`MainActivity.kt`、`Chrome.kt`、`AppRoute.kt`、`AppNavigatorTest.kt`、`ProjectComponentsTest.kt`）：提交时只 `git add` 本任务相关文件，绝不提交或还原这六个文件中的未暂存改动。
3. 仅本地提交，不 `git push`。
4. 默认调用保持现有自动幂等键行为；显式键是新能力，不改既有调用点语义（除本计划明确列出的调用点）。
5. `ImportAttempt`/`ReviewAttempt`/`PdfUploadAttempt` 在配置变更（旋转）后存活（ViewModel 持有）；进程终止后不自动重放，先刷新服务端状态。
6. 缺失服务端指标的视觉位置保留，统一显示 `—`；不隐藏模块，不借用无关字段。
7. 小于 1000 的真实计数直接显示整数；`0.042k` 一类误导性缩写禁止出现。
8. 不清理旧 Room/排程代码，不重做登录、项目/PDF、智能制卡流程。
9. 测试命名 `test_<模块>_<行为>`；构建走 CLAUDE.md 记录的 Windows JDK 通道（`cmd.exe /c "set JAVA_HOME=C:\Users\admin\.gradle\jdks\eclipse_adoptium-21-amd64-windows.2 && call gradlew.bat ..."`，先把 `Front` 同步到 `/mnt/c/Users/admin/Documents/ChatGPT/闪卡app/`）。
10. API key / 明文凭据不得进入日志、fixture、提交信息。
