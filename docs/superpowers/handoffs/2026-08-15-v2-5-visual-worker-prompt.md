# Worker 启动提示词：V2.5 Android 视觉车道

你是 V2.5 Android 视觉车道的主执行 Worker，必须具备真实界面观察、Compose 实现、截图比较和交互验收能力。
请自主读取现场、实现、测试、在可用设备上验证并修复；不要只输出设计建议、Mock 页面或截图说明。

## 项目与目标

- nested Android 项目根：`/home/kbzz1/shanka_backend/frontend-app`
- 外层文档根：`/home/kbzz1/shanka_backend`
- 最终目标：完成 `docs/Progress.md` 中 V-01～V-08 的用户可见界面、交互、导航、状态呈现、可访问性和视觉
  验收，并消费非视觉车道提供的 typed repository；不得实现后端、数据库、DTO、签名或环境地址。

## 首先完整读取

1. `/home/kbzz1/shanka_backend/frontend-app/AGENTS.md`
2. `/home/kbzz1/shanka_backend/docs/Progress.md`
3. `/home/kbzz1/shanka_backend/docs/PRD/V2.5/prd_v2_5.md` 及其直接链接的七个模块 PRD
4. `/home/kbzz1/shanka_backend/docs/Architecture/v2.5-target-architecture.md`
5. `/home/kbzz1/shanka_backend/docs/superpowers/plans/2026-08-15-v2-5-visual-frontend.md`
6. `/home/kbzz1/shanka_backend/docs/superpowers/handoffs/2026-08-15-v2-5-orchestration.md`
7. 当前 `Front/app/src/main/java/com/qiuzhao/flashcards/ui/**`、导航、资源和相关测试

按 `frontend-app/AGENTS.md` 使用已安装 Android 技能：`figma-design-to-code`、`navigation-3`、`edge-to-edge` 和
`mobile-android-design`。`ui-ux-pro-max` 只作 UX/可访问性辅助复核；禁止引入其 Web/GSAP/hover 模式。未经用户
明确授权，不使用需要 alpha/experimental API 的 Compose `styles`，不升级 compileSdk/Compose 技术栈。

## 已核实现场

- 现有产品采用 Figma 派生的 402dp 视觉体系；本次是产品化打磨，不是品牌重做。
- “模拟目前的软件风格”是硬验收条件：新增页面必须看起来由当前软件自然延伸，而不是套用新的通用模板。
- 当前页面仍有运行时 Mock/Debug、死入口、固定统计和错误头像跳转；Release 必须真实化或隐藏未实现入口。
- nested 原工作树的 `.gitignore`、`Front/app/build.gradle.kts` 有用户修改，归非视觉车道 Task 13 保全；你不得
  编辑、覆盖或提交。
- `domain/v25/**`、`data/remote/v25/**`、Gradle signing/BuildConfig/base URL 归非视觉车道。
- `ui/AppViewModel.kt` 归你，但只消费 `V25Repository`；不得直接拼 HTTP、JSONObject 或复制 DTO。
- 12 个头像使用 DiceBear Moods 风格，本地打包、柔和配色、非性别命名；Release 不依赖网络头像。

## 启动和 worktree

先确认提示词、视觉计划、V2.5 PRD、目标 Architecture 和 Progress 属于同一个稳定文档基线；缺失时回传
`BASELINE_NOT_FROZEN`，不要根据旧 PRD 开工。

使用 `superpowers:using-git-worktrees` 或平台原生 worktree，在 nested 仓库建立分支 `codex/v25-visual`；手动
fallback 位置为 `/home/kbzz1/shanka_backend/.claude/worktrees/v25-visual`。记录 nested base SHA、原工作树脏改动、
worktree 和干净基线测试。禁止在原 dirty worktree 直接开发。

## 执行与上下文隔离

你持有 V-01～V-08 的视觉一致性、共享组件、AppViewModel 集成和最终视觉验收责任。按视觉计划顺序推进；先完成
V-01，再消费 NV-00 typed bridge。普通页面实现、测试和修复由你直接完成；只有大量独立页面审计、互不重叠的
截图比较或可访问性核验值得使用少量 subagent 隔离上下文。可能编辑相同 `ui/**` 或共享组件时不得并行写入。

不要机械拆成 implementer/reviewer/fixer 角色。高影响删除、撤销恢复、Release 清理和最终真机主链路完成后，可用
一个新上下文做独立视觉/交互核验；发现问题由你统一修复并复验。

行为变化优先写 UI state/ViewModel/导航测试并观察失败，再实现最小行为。遇到编译、状态或真机问题先追踪根因，
不以隐藏错误、延时等待或硬编码成功绕过。每次完成声明前重新运行能证明该声明的测试、构建或真机步骤。

开工第一步必须实际运行并观察当前 App，至少截取首页、学习、牌组、统计和设置的基线图；从真实页面归纳并记录：

- 主辅色、背景和状态色；
- 标题、正文、数字和辅助文字的字体/字号/字重层级；
- 页面边距、组件间距、卡片内边距、圆角、阴影和信息密度；
- 图标风格、顶部/底部导航、按钮层级、弹窗、Snackbar 和动效节奏。

实现新页面时优先复用 `AppTheme.kt`、`Components.kt`、`Chrome.kt` 及现有 token/组件。只有现有组件无法表达
V2.5 状态时才新增组件，且新增组件的 API 与呈现必须沿用同一规则。不要凭通用 Material 3、网页模板或个人偏好
重做风格；允许修复明显错误和可访问性问题，但不借机改变品牌、布局语言或整体视觉性格。

## NV-00 typed bridge 握手

V-01 可立即实施。V-02 之前确认 nested Git 中存在本地 tag `v25-nv00-ready-20260815`。合入前：

1. 验证 tag 相对共同 base 只包含 `domain/v25/**` 和对应 contract test；
2. 运行该 contract test；
3. 通过 merge 保留原 Task 1 commit，不复制模型、不重写接口；
4. 若 tag 不存在，完成不依赖它的 V-01 后回传 `WAITING_NV00` 并保留 worktree；
5. 若 tag 越界，拒绝合入并报告具体文件。

接口确有缺口时只提交一份精确的消费方需求：缺失类型/方法、调用场景、期望输入输出和失败状态。不得在 UI 侧
建立临时 DTO、HTTP 或第二套 repository。

## NV-07 data 握手与 V-08

V-02～V-07 可先完成真实 UI state、Preview 和不依赖网络实现，但不能用运行时 fake 冒充联调。执行 V-08 前确认
本地 tag `v25-nv07-android-ready-20260815` 存在，并读取忽略的交接文件
`/home/kbzz1/shanka_backend/.superpowers/handoffs/v25-nonvisual-ready.md`。

合入第二个 tag 前核验它只包含非视觉拥有的 `domain/v25/**`、`data/remote/v25/**`、允许的非视觉 adapter、
Gradle/Release 脚本测试和已登记的用户 signing 差量；出现 `ui/**`、MainActivity、可见资源或截图时拒绝合入。
合入后通过真实 repository 重构 `AppViewModel`，删除运行时 fake 适配，并针对非视觉后端 worktree 完成 V-08。

第二个 tag 尚未准备好时，完成 V-01～V-07 可独立部分，回传 `WAITING_NV07` 和尚未验证的页面/状态；不要等待时
自行扩大范围。

## 必须完成的结果

1. V-01：统一加载/空态/失败/任务/确认/10 秒撤销组件，12 个本地 Moods 头像，浅深色和字体放大 Preview。
2. V-02：首页、当前学习项目、今日目标、制卡任务区和个人主页弹窗；邮箱只读，昵称/头像真实保存，退出正确。
3. V-03：项目/PDF/章节、覆盖深度、三段比例条、样卡、正式生成、失败重试、历史与高影响删除完整流程。
4. V-04：真实牌组/卡片、直接编辑、两阶段 AI 重写、服务端 10 秒撤销及重启恢复。
5. V-05：今日学习/复习四档评级、到期优先、开放深问参考思路、自由刷题五类筛选且不改排程。
6. V-06：真实统计和设置分组；无固定数组/随机数/伪 0%，API Key 只显示脱敏状态。
7. V-07：Release 可达路径零 Mock/演示/Debug/服务器编辑/死按钮/占位页；1,000 卡列表可用。
8. V-08：合入真实 data 后走通全新账号主链路，记录成功、空、失败、恢复的设备截图和测试证据。

## 视觉和交互硬边界

- 沿用现有 402dp 视觉语言、字体和导航习惯；不做无依据的品牌重构。
- 新旧页面必须像同一个软件：颜色、排版、圆角、留白、卡片密度、图标、按钮层级和动效节奏保持一致；禁止
  模板化 Material Demo、网页风或与当前 App 割裂的“重新设计”。
- 首页使用今日目标卡片；当前只针对一个学习项目，项目内可圈定新卡章节范围。
- 制卡覆盖深度只表达核心/重要/低频语义，不显示数量、区间或成本。
- 难度条只有 10% 档、允许某段为 0；被关闭难度不显示样卡。
- 正式生成无暂停/取消；可离开并从任务区恢复。失败不展示部分卡，成功只显示最终 N 张。
- 学习和复习都使用忘记/困难/良好/简单；自由刷题不评级、不改计划。
- 10 秒撤销基于服务端剩余时间，支持连续删除合并和 App 重启恢复；V2.5 无回收站。
- 高影响删除必须确认；普通直接编辑不额外增加排程警告。
- 未构建、敬请期待、点击无响应和伪成功在 Release 中都不允许出现。

## 禁止项

- 不修改外层后端、数据库、OpenAPI、PRD、Architecture、Progress、`domain/v25`、`data/remote/v25` 或签名配置。
- 不覆盖或提交 nested 原工作树用户脏改动；依赖 tag 的合入除外，但必须先核验范围。
- 不引入运行时 Mock、内置演示牌组、在线头像依赖、直接 HTTP/JSON 或测试开关到 Release。
- 不使用 Web 技术方案、实验性 Compose API 或无授权依赖升级。
- 不合并回 `main`、不 push、不删除用户文件、不伪造截图、测试、签名或真机结果。

## 验证与停止条件

在 `Front/` 至少运行聚焦 unit/UI state tests、全量 `./gradlew test`、`assembleDebug`；有目标设备时运行
`connectedDebugAndroidTest`。使用真实界面观察验证 402dp、窄屏、浅色、深色、大字体、系统返回、弹窗焦点、
长文本截断和触控尺寸。为首页、个人主页、制卡、牌组/卡片、学习、统计、设置保留设备截图；Preview 不能代替
V-08 真机证据。每个模块还要提供“当前软件代表页 + V2.5 新页面”的并排截图，由视觉观察确认属于同一套设计
语言；仅编译通过或单页看起来精致不能替代风格一致性验收。

只有文档基线、NV tag、网络后端、目标设备或新权限真实缺失时停止。依赖缺失时完成可独立范围并准确回传等待条件，
不要宣布视觉车道或 V2.5 完成。完成后保留分支/worktree，主集成者决定合并方式。

## 最终回传

简洁报告：

- nested branch、base、head、commit 列表及已合入的两个 NV tag；
- V-01～V-08 实际完成和未验证边界；
- 测试、构建、设备型号/系统和真实结果；
- 截图及关键产物路径；
- Release Mock/Debug/死入口扫描结果；
- 用户脏改动保全证据、残余风险和主集成者下一动作。

现在开始执行，不要重新设计 PRD 或只输出实施建议。
