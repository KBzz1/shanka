# V2.5 视觉前端实施计划

> 交给具备视觉能力的 Android Agent。只负责用户可见界面、交互呈现、导航和视觉验收；不得实现后端、数据库、
> OpenAPI、网络 DTO、签名或环境地址。状态以 `docs/Progress.md` 的 V-LANE 为准。

## Goal

在现有 402dp Figma 视觉体系上完成 V2.5 Release 的全部可见页面和状态，清除 Mock、Debug 与死入口，并消费
非视觉 Agent 提供的 `domain/v25` Repository 接口。不是重新设计品牌，不启用实验性 Compose/compileSdk 升级。

## 现有软件风格保真

- 开工先实际运行并截图审计当前首页、学习、牌组、统计和设置页；归纳现有颜色、字体层级、间距、圆角、阴影、
  卡片密度、图标、导航和动效节奏。不得只根据通用 Material 3 经验猜测。
- 新页面与新状态必须模拟当前软件已经形成的视觉语言，优先复用 `AppTheme.kt`、`Components.kt`、`Chrome.kt`
  及既有组件，不另建一套平行设计系统。
- 产品化允许修正明显的不一致、可访问性和死代码，但不得把 V2.5 做成风格突变的全新 App、模板化 Material
  Demo、网页风界面或过度装饰的 AI 设计稿。
- 每个模块验收包含“现有代表页 + V2.5 新页面”的并排截图；若字体、色彩、留白、圆角、信息密度或导航层级
  看起来不属于同一软件，任务不得通过视觉验收。

## 工作区与文件所有权

- 嵌套仓库：`/home/kbzz1/shanka_backend/frontend-app`；视觉 Agent 使用独立分支/工作树 `codex/v25-visual`。
- 开始前记录 `git -C frontend-app status --short`；现有 `.gitignore`、`Front/app/build.gradle.kts` 脏改动不属于视觉
  Agent，禁止覆盖或提交。
- 独占：`Front/app/src/main/java/com/qiuzhao/flashcards/ui/**`、`MainActivity.kt`、可见 strings、drawable、主题、
  Compose UI test 和截图基线。
- 允许修改 `ui/AppViewModel.kt`，但只通过 `domain/v25` 接口编排 UI state；不得直接拼 HTTP/JSON。
- 禁止修改：外层 `main/**`、Architecture、PRD、Progress、`data/remote/v25/**`、`domain/v25/**`、Gradle 签名/
  BuildConfig/base URL。
- Preview 可以使用 `@Preview` 本地样例；Release 运行路径不得包含 `FrontendTestFixtures`、`BuiltinDecks` 或测试开关。

## 依赖握手

- 可立即开始 V-01 的纯视觉组件和 Preview。
- V-02 起消费 [V2.5 目标 Architecture](../../Architecture/v2.5-target-architecture.md)的状态模型。
- 真实联调前必须等待非视觉 Agent 完成 NV-00（`domain/v25` 接口）和对应后端工作包。
- 接口不满足时登记到 Progress 风险区，不在 UI 层复制 DTO 或临时发 HTTP。

---

## V-01 视觉基础、Moods 头像与状态组件

**Files**

- Modify: `ui/AppTheme.kt`, `ui/Components.kt`, `ui/Chrome.kt`, `ui/motion/AppMotion.kt`
- Create: `ui/v25/components/*`, `res/drawable/mood_01..mood_12.*`
- Modify: `res/values/strings.xml`

- [ ] 固化加载、空态、可恢复失败、不可恢复失败、后台任务、确认弹窗和 10 秒撤销条的统一组件。
- [ ] 打包 12 个 DiceBear Moods 预设资源；统一柔和色系，不依赖网络，不使用性别名称。
- [ ] 浅色/深色、字体放大、系统返回、触控尺寸和仅靠颜色不可辨识问题通过检查。
- [ ] 为核心组件建立 Preview 矩阵，不接入运行时 Mock。
- [ ] 运行 `./gradlew test` 与 Compose 编译检查；只提交视觉 Agent 所有文件。

## V-02 首页、学习项目入口与个人主页

**Files**

- Modify: `ui/HomeScreen.kt`, `ui/SettingsScreen.kt`, `ui/AppViewModel.kt`, navigation files
- Create: `ui/v25/ProfileSheet.kt`, `ui/v25/ProjectSwitcher.kt`, `ui/v25/TaskSection.kt`

- [ ] 首页头像在有效会话打开个人主页弹窗；失效会话由既有 auth 状态进入登录页。
- [ ] 个人主页展示头像、昵称、只读完整邮箱；支持昵称编辑、12 头像网格、退出登录及所有失败状态。
- [ ] 首页今日目标使用真实 `TodayStudyPlan`：项目名、完成/目标、到期数和正确主按钮。
- [ ] 学习页增加“制卡任务”区域，区分项目状态和任务状态，展示继续/失败/已放弃/历史入口。
- [ ] 未选择项目、无项目、项目无卡和数据刷新失败使用不同空态。
- [ ] 增加 ViewModel 状态测试：资料保存失败保留输入、项目切换、中断恢复。

## V-03 学习项目与制卡任务全流程

**Files**

- Refactor: `ui/PdfMaker.kt`, `PdfPreview.kt`, `PdfSettings.kt`, `PdfTask.kt`
- Create: `ui/v25/project/*`, `ui/v25/generation/*`

- [ ] 上传成功后进入项目详情；默认项目名可编辑，退出/返回不清空服务端草稿。
- [ ] 章节确认支持修改、选择和高影响删除确认；活跃任务阻塞时显示具体任务与可行动作。
- [ ] 覆盖深度只展示“精简/均衡/充分覆盖”的语义，不出现数量和成本。
- [ ] 难度使用三段比例条+两个拖动点，10% 档、允许 0、相邻段联动、无小数。
- [ ] 样卡只显示启用难度的 1～3 张；提供正式生成、改配置重生成、放弃。
- [ ] 正式生成页无暂停/取消；可安全离开，并从任务区域查看后台状态。
- [ ] 失败页只给真实原因和“重新生成”；成功页只显示“已完成，共生成 N 张”。
- [ ] 项目、章节、任务删除确认完整呈现保留/删除卡片的影响数量。

## V-04 牌组、卡片、AI 重写与删除撤销

**Files**

- Refactor: `ui/LibraryScreen.kt`, `DeckScreen.kt`, `CardListScreen.kt`, `AddCardImportScreen.kt`
- Create: `ui/v25/cards/*`

- [ ] 牌组列表/详情全部真实化；项目牌组、独立牌组和“未归属章节”清晰但不过度抢视觉。
- [ ] 卡片正反面直接编辑，保存失败保留输入；不增加重置排程二次弹窗。
- [ ] 来源可用的生成卡展示“AI 重新生成”；生成预览后才能替换，取消/失败保留原卡。
- [ ] 单卡删除展示服务端剩余 10 秒；连续删除合并数量；页面切换、后台、App 重启后恢复撤销条。
- [ ] 离线删除失败时卡片留在列表；撤销超时后不展示回收站入口。
- [ ] 牌组、任务结果和项目删除使用高影响确认，不误用 Snackbar 代替确认。

## V-05 学习、复习与自由刷题

**Files**

- Refactor: `ui/StudyScreen.kt`
- Create: `ui/v25/study/*`

- [ ] 今日队列展示正面→翻面→忘记/困难/良好/简单；网络失败停留当前卡，提交成功才前进。
- [ ] 到期优先、新卡补足、积压继续复习和今日完成状态与服务端计划一致。
- [ ] 开放深问背面明确“参考思路”，不呈现唯一答案暗示。
- [ ] 自由刷题支持正序、会话内稳定随机、内容难度四类、未掌握、已掌握。
- [ ] 自由刷题不显示评级控件，不改变首页完成数；独立牌组可直接进入到期复习。
- [ ] App 被中断、卡被其他设备删除和重复点击评级均有可解释恢复。

## V-06 统计页与设置页产品化

**Files**

- Refactor: `ui/DataScreen.kt`, `ui/SettingsScreen.kt`
- Create: `ui/v25/stats/*`, `ui/v25/settings/*`

- [ ] 沿用现有统计页视觉层级，但移除固定数组、固定日期、随机值和伪 0%。
- [ ] 明确区分评级次数、去重完成卡数、周目标完成率和掌握卡数。
- [ ] 空账号、上周无分母、刷新失败和暂未更新状态正确。
- [ ] 设置页分组：个人资料、制卡默认值、学习计划、AI 服务、外观。
- [ ] 学习时区支持首次建议、设备不同时二选一和主动修改；未经确认不静默变化。
- [ ] API Key 只展示脱敏状态；Release 不显示服务器地址、Debug、测试或占位设置。

## V-07 Release 视觉清理、可访问性与性能

- [ ] 删除/隔离 Release 可达的 `FrontendTestFixtures`、`FRONTEND_TEST_MODE`、内置演示牌组和旧动漫头像引用。
- [ ] 对所有可见按钮做真实能力审计：可用或隐藏，不留未构建、敬请期待、空白页和伪成功。
- [ ] 在 402dp 基线及窄屏/大字体/深色模式检查截断、遮挡、返回和弹窗焦点。
- [ ] 1,000 卡列表使用稳定 key 和惰性列表；加载与后台生成期间导航可操作。
- [ ] 为首页、个人主页、制卡、牌组、学习、统计、设置建立截图证据和关键 Compose UI 测试。
- [ ] 将 V2.5 新页面与当前软件代表页面并排检查，确认主题 token、排版、圆角、留白、卡片密度、图标和动效
      节奏属于同一视觉体系。
- [ ] 运行 `./gradlew test`、`assembleDebug`；有目标设备时运行 `connectedDebugAndroidTest`。

## V-08 真实接口集成与视觉验收

- [ ] 合入非视觉 Agent 已稳定的 `domain/v25` 与 data 实现后，移除所有运行时 fake 适配。
- [ ] 全新账号走通个人主页→项目→样卡→生成→学习→统计→撤销→重启恢复。
- [ ] 记录每个模块的成功、空态、失败和恢复截图；不以 Preview 代替真机证据。
- [ ] 将提交哈希、测试结果和未通过项交给主集成者；视觉 Agent 不自行更新 Progress 为 DONE。

## 视觉车道 DONE 门槛

- V2.5 所有可见入口均有真实状态映射，无 Mock/Debug/死入口；
- 浅色、深色、大字体和目标设备主链路通过；
- 不包含网络协议、数据库或签名的越界实现；
- 未覆盖用户现有的 nested repo 脏改动。
