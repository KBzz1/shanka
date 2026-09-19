# 闪卡 App V2.5 执行地图

本文是 V2.5 唯一进度、依赖与 DONE 事实源。需求权威为
[V2.5 PRD](PRD/V2.5/prd_v2_5.md)，目标技术设计为
[V2.5 Architecture](Architecture/v2.5-target-architecture.md)（已转正，现行契约见
`Architecture/structure-contract.md` / `openapi.yaml` / `database-design.md`）。
两份实施计划只负责细化任务，不得各自另建总状态表。

最后事实审计：2026-09-04（第五次）。审计手段：全量后端测试复跑
（**867 通过 / 0 失败 / 867 项**；ruff + mypy strict 全绿）+ 安全/部署加固批——
限流客户端 IP 确定性采信 `CF-Connecting-IP`（`app/middleware/client_ip.py`，Tunnel 部署
键维度修正）；`/metrics` 默认 Bearer 收紧（`METRICS_AUTH_EXEMPT` 开关，测试断言更新）；
单进程约束显式写入 run.sh 与 deployment.md（uvicorn 显式 --proxy-headers）；R25-07
并发压力测试落地（HTTP 端到端，详见风险表）；GitHub Actions CI 上线
（`.github/workflows/ci.yml`：ruff/mypy/契约守卫快通道 + 全量 pytest，依赖按
`requirements*.lock` 锁装）+ 生产锁文件 `requirements.lock`。前次审计（第四次，
2026-09-04）：860 项全绿 + openapi.yaml 路径覆盖补齐（47/47 业务端点双向守卫）+
R25-10 material deletion-preflight + A5 文档核对 + fake.py 清理。

---

## 1. 状态规则

- `DONE`：要求已实现，约定验证实际通过，证据路径和结果已登记。
- `DOING`：主执行者已开始，必须注明工作树/分支和当前边界。
- `TODO`：尚未开始，或只有文档/页面/测试名而无真实实现。
- `BLOCKED`：存在无法在当前授权内解决的外部条件；不得把困难、工作量大或等待另一车道依赖标为 BLOCKED。

文档确认、代码存在、测试名称、旧 APK 和 Mock 页面分别只是对应层级的证据，不能互相替代。

---

## 2. 当前基线

| 范围 | 状态 | 当前事实 |
| --- | --- | --- |
| V2.5 总 PRD + 7 模块 PRD | `DONE` | 已确认 v1.0；8 文件内部链接和 diff 格式检查通过 |
| V2.5 目标 Architecture | `DONE` | 已转正（2026-08-31）：目标设计已原子同步到现行契约，状态头与转正记录见 `Architecture/v2.5-target-architecture.md` |
| 当前机器契约 | `DONE`（V2.5 实现事实） | `structure-contract.md`（v2.5）、`openapi.yaml`（2.5.0，2026-09-04 路径覆盖补齐；2026-09-08 确认闭环新增 confirm/task cards 两端点，业务端点 50 个，probes/metrics 按 R-04 有意排除）、`database-design.md`（v2.5）均自标 V2.5 实现事实，contract 守卫套件通过（含 app 路由 ↔ openapi 双向路径守卫） |
| V2.5 多资料项目（V25-D-29~32） | `DOING` | A1 契约 + A2/A4 后端（迁移 b7e4c2a91d50，862/862 绿，commit ed7a5a9）+ A3 前端翻面（236 单测绿 + assembleDebug，Room 显式 Migration 1→2）已落；A5 文档收尾完成（2026-09-04：backend-integration.md 多资料端点核对——两步创建/materials 系列/material replace、offline-data-layer.md Room 15 表投影核对一致；R25-10 preflight 缺口同批关闭）；真机验收证据未登记 |
| 生成结果确认与延迟发布（AWAITING_CONFIRMATION 八态，2026-09-08 交接 §5） | `DONE`（后端） | Phase 0 后端全量落地：迁移 `3e7b9d2c5f14`（八态 CHECK）+ executor park（替代自动发布，PUBLISHING 孤儿接管落点=待确认）+ `POST /tasks/{id}/confirm`（单事务发布）+ retry v2（FAILED 原语义 + 待确认 supersede 单事务硬删 STAGED）+ `GET /tasks/{id}/cards`（复审只读出口）+ `GET /tasks?deck_id=`；契约四件套（openapi/structure-contract/database-design/PRD FR-06+AC-03）与 backend-integration.md 同批更新；全量 pytest 通过（2026-09-08）；前端（卡组四态/复审页/确认闭环）并行开发中，未联调 |
| V2.5 后端/数据库实现 | `DONE` | V2.5 契约迁移已落（`0f8b9f33b769_v2_5_contract` 等共 15 个迁移，含 `a3f8d21c9e47` coverage_tier）；projects/study/tasks 七态/删除批次/重写预览等 V2.5 服务与路由齐全；密度制数量编排上线（V25-D-25~28，`knowledge_points.coverage_tier` 已补应用到运行库）；2026-08-31 全量 pytest 868/868 通过，真实验收证据见 `Architecture/generation-quality-metrics.md` |
| 两阶段规划 V2.5.2（粗规划主题盘点 + 精规划主题展开，2026-09-09） | `DONE`（后端） | 规划拆两阶段修复档位无区分/COMPACT 混层/充分欠产：资产 v7（`prompts/planner-coarse`、planner→精规划、`planner_output` v7 units 含 topic_index 无 coverage_tier、新增 `planner_coarse_output`）+ `coarse_validator.py`（tier∈模式允许集确定性过滤/上限截断/标题去重/topic_index 分配）+ `planning_executor.py` 两阶段编排（账本/预算/心跳全复用，operation_key `planning:coarse/fine/fine-wide`，空产出主题整章恢复）+ 契约同步（structure-contract §2/§3.5/§3.6/§4.1/§4.2/§8.5、database-design operation_key）；依据子代理消融实验（`scripts/planning_ablation/report.md`：8/8 cell 命中区间、同章三档 25/49/81 vs 旧 19/19、tier 违规 0、B3 0.86~1.0，**density_assessment 变体不采纳**）；全量 pytest 通过（2026-09-09，~903 项）+ ruff/mypy strict 绿；真机 B5 专项验收决策跳过（2026-09-10：消融证据已足，档位区分/密度区间由两阶段结构 + 服务端确定性过滤保证，上线后由质量指标观测承接） |
| V2.5 Android 视觉实现 | `DOING` | 视觉实现已大量落地：UI 拆分为约 30 个屏幕文件、design-system 402dp 体系应用、真机截图证据在 `releases/visual-evidence/`；但 V-01~V-08 逐包验收证据（Preview 矩阵、浅深色/大字体、UI tests）未登记 |
| V2.5 Android data/Release | `DONE` | offline-foundation-v1 设备验收关闭（commit 2bbd080）：统一 NetworkStack + Room 投影 `shanka-v25.db` + 评分 outbox（`docs/frontend/offline-data-layer.md`）；debug 与正式包安装隔离（commit 1d175a6）；Release 编译期固定 `https://shanka.kbzz1.top`；Release APK 2.5.0 + SHA-256 已产出（`releases/`） |
| 版本管理规范化 + 启动器图标（2026-09-14，V2.5.2 交付） | `DONE` | 版本单一事实源 `appVersionName`（`Front/app/build.gradle.kts`，2.5.2）+ `versionCode` 派生规则（2.5.2 → 20502，不再手工维护）+ `ReleaseConfigTest` 改锁流程（声明/引用/派生三断言）+ `build-release.sh` 门禁升级（解析声明版本、aapt 校验 versionName+versionCode+图标资源、版本化归档 `shanka-v2.5.2-release.apk`、git tag `v<version>` 唯一性防重复发版）；`release-and-acceptance.md` §1 版本策略条款同步。首次配置启动器图标：源图 `Front/design/app_icon_source.png` + 生成脚本 `Front/tools/generate_launcher_icons.py`（自适应裁边 143px、满铺前景 + 边缘均色背景层、anydpi-v26 + 五密度兜底位图、预览 `icon_preview.png`）+ Manifest 挂载 icon/roundIcon。Release APK 2.5.2（versionCode 20502，SHA-256 `e4eaf30f…`）已产出并归档 `releases/`，git tag `v2.5.2` 已打；同日按用户反馈修复：满铺图标主体过大 → 主体缩至画布 64% 留蓝色边距（背景改边缘拉伸铺满+强模糊，模拟器实测通过） |
| 学习会话与评分来源（2026-09-17，V25-D-37） | `DONE`（后端+契约+前端代码；真机验收未登记） | 薄会话资源 `StudySession`（自然键 用户×origin(PLAN/BACKLOG/ADHOC)×范围×学习日，同日续用同一行=当日中断续学，跨天自动新行）+ `review_events.origin`（可缺省，NULL=未分类保旧客户端过渡期）+ 看板时长聚合（周合计/每日 7 桶/三来源拆分）。产品语义三句话入 PRD D-37：状态在卡上、事实在流水里、会话只是当天的门。契约四件套同步（PRD V25-STUDY-FR-11/AC-05、V25-STATS-FR-07/AC-04、structure-contract 3.25/3.11/6.6/§7 SESSION_NOT_FOUND、openapi 三端点+ReviewOrigin 枚举、database-design 2.23/7.6）；后端迁移 `e6a9c3f02b7d`（review_events.origin + study_sessions 表）+ `/study/sessions` begin/report/list（幂等键 + 绝对值 max 合并）+ stats 时长聚合，全量 pytest 956/956 通过（2026-09-17）；前端评分 outbox 带 origin（Room v8：outbox origin 列 + dashboard 时长列，显式 Migration 7→8）、StudyScreen 三入口 begin/report（今日计划→PLAN、积压→BACKLOG、卡组开始学习→ADHOC；自由刷题不产生会话）、drained pass 尽力补发；看板时长数据经 stats/dashboard 接口与 DashboardUiState 字段下发，统计页不新增独立时长卡（用户裁决 2026-09-17）；同日二次裁决：项目/卡组详情页既有「学习时长」卡直接切换为服务端会话累计（新增 `GET /study/sessions/summary`：ADHOC 按卡组 + 三来源全历史合计），设备本地全量秒数链路（deck_study_seconds 表/DAO/observeStudySeconds）随 Migration 7→8 删除，今日 tab 的按日本地实测保留。同日三次裁决：卡组学习页右上角新增会话重置按钮（`restart_alt`，仅 ADHOC）：begin 请求 `reset=true` → 当日活跃 ADHOC 会话封存（时长永久保留、summary 合计不受影响）、按 deck_key 代数后缀 `deck_id#N` 开新一代从 0 计时，响应携带全卡组复盘队列（新卡按 position 最前，其余按 FSRS 遗忘风险降序；评分照常走排程，不改写任何卡片事实）；集成测试 test_reset_seals_old_session_* 锁语义。**部署顺序硬约束：后端先发（origin 可选），APK 后发**。ProjectDetail 今日时长仍本地口径（决策：看板先行） |
| 账号级跨项目学习计划（2026-09-19，V25-D-39） | `DONE`（后端+契约+前端代码；模拟器 E2E 通过，真机验收未登记） | 显式修订 V25-D-03 单项目规则：今日计划升级为**账号级一份**（双目标 + 卡组集合），可选任意本人卡组（跨项目与独立卡组）；`PUT /study/plan` 去 project_id（旧客户端多发字段被 pydantic 忽略）、保存不再改写 `user_preferences.current_project_id`（仅显式 PATCH 控制）；`/study/today`、`/study/plan`、backlog 响应去 current_project 字段（旧 APK DTO nullable 默认值容忍，已核实全仓零 UI 消费）；今日计划 legacy 章节回退分支退役（无计划=诚实空态 plan_configured=false）。**端点退役**：GET/PATCH /projects/{id}/study-settings、GET /projects/{id}/stats/weekly（前端从未调用，项目周目标语义随账号级计划失效，用户级周活跃由看板承接）。**语义裁决**：retain_decks=true 删项目后卡组转独立并继续留在计划（翻转旧行为，PRD 明写）；项目详情"学习时长"不再计入 PLAN/BACKLOG 时段（跨项目归属不定），仅 deck 级合计。契约四件套同步（PRD V25-D-39 决策条目+五份模块 PRD 的 FR/AC/非目标改写、structure-contract 3.15/3.17 墓碑/3.17.1 重写/3.20/3.23 墓碑/6.x/§7/§8、openapi 删两路径两 schema+计划三 schema 去项目字段、database-design 2.19/2.19.1 换 user_study_* 表+§1/§3/§6/§7.7、Architecture/AGENTS C-08 决策、frontend/backend-integration §3.8）；顺手修 structure-contract 6.x 接口清单 /v1 前缀漂移。后端：迁移 `a7c4e9f1b3d5`（建 user_study_settings/user_study_decks → 两层回填：优先 current_project_id 项目计划、无则该用户 updated_at 最新计划行 → drop 两旧表，不可逆）+ study service 五函数切 user 级 + 删 legacy 分支 + projects service 清播种/设置读写/章节范围联动 + progress 删 weekly。前端：模型/DTO/仓库去项目字段与 study-settings 方法、Room Migration 8→9（study_plan/today_plan 重建去 current_project 列，schemas 9.json）、StudyGoalScreen 撤 2.5.8 的单项目互斥恢复跨项目多选（canSave 去 hasProject）、AppViewModel/Chrome/ProjectDetail 去归属门。验证（2026-09-19）：后端全量 pytest 974/974 + ruff/mypy 绿；前端 gradlew test 绿。发布（2.5.9）：**后端先发（含迁移）→ APK 后发**；已知窗口期：混合版本多端保存会互相截断计划范围（旧端只提交单项目），全量升级后自愈 |
| 章节规划与资料统一分诊（2026-09-16~17，V25-D-36 + V25-D-38） | `DONE`（后端+契约+前端代码+离线评测；真机验收未登记） | **D-36 无目录 PDF 的 AI 章节规划**：分诊后仅"无目录且超阈值"PDF 走 AI——按约 2.4 万字符分段调 chapter_planner 识别章节起始点（`services/chapters/`：planner 分段编排+0 边界引导重试一次、validator 确定性校验页码/标题/上限、triage 阈值分诊），账本新 stage=`CHAPTER_PLANNING`（scope=MATERIAL，operation_key `chapters:{material_id}:{seg}[:guided]`）；AI 失败两条出路均不重传文件：`POST materials/{id}/reparse` 重试、`POST materials/{id}/chapters/whole-book` 整本单章（source=FALLBACK）；错误码 `PDF_AI_CHAPTERS_FAILED`/`API_KEY_NOT_SET`。资产 chapter_planner prompt **v9→v10**（边界判定从形态枚举改分层原理：编号体系按跨度/频次分层、中文序号同权、0 边界引导重试）+ chapter_planner_output schema v9；两书量化评测（`scripts/chapter_planning_eval`，deepseek-flash 实测）：闪卡书 F1 0→**0.952**、样书 F1 0.512→0.377~0.431（多切为 flash 规模模型固有倾向，根治方向=架构层确定性分层，列后续工作包候选），结论与取舍见 `agent_evolution/CHANGELOG`。**D-38 资料统一文本化与确定性分诊**：资料归一 text_chunks+结构候选后程序优先——自带结构直用（PDF 目录/HTML 标题/ZIP 文件夹）、总字数 ≤ `single_chapter_max_chars`=24000 直接单章（source=AUTO 零模型零 Key）、HTML 无标题结构恒单章；新增 HTML 资料类型（`POST materials/html` 同步解析即时就绪、最浅标题级即章节 source=HEADING、不存档原件、标准库 html.parser 零新依赖，`services/projects/html_archive.py`）。迁移 `d4f8a2c6b1e9`（chapters.source 列+存量按资料类型回填、llm_call_attempts stage 域）+ `e5c1d9b7a3f2`（source 域扩 HEADING/AUTO），均不可逆（降级走备份恢复）。前端：Room Migration 6→7（chapters source 列）、HTML 上传入口（PdfUploadCoordinator/MaterialImportScreen）、AI 章节失败重试/整本单章降级 UI（AiChapterRetryableTest 锁行为）。契约四件套同步（PRD 1.1/旅程/D-36/D-38、structure-contract 3.2 规则重写+3.2a HTML+3.3 source+6.2 端点、openapi 4 新端点+ChapterSource 枚举、database-design 章节表/账本域/§7 迁移）；同批修正决策编号撞号（学习会话误标 D-38 → 统一 D-37，PRD/structure-contract/database-design/openapi/代码注释 21 处）。验证（2026-09-17）：全量 pytest 973 项——972 过 + 1 既有随机抖动 `test_free_browse_random_order_session_stable`（seed=sha256(user:deck)，5 张牌 1/120 恒等排列碰撞，与本次改动无关；改 10 张压平后复跑绿）+ ruff/mypy strict 绿；前端 gradlew test 绿。发布：与 D-37 同批发版（2.5.6），后端先发、APK 后发 |
| V2.5 正式发布 | `TODO` | APK 产物已存在，但 G2~G5 证据链（模块真数据四态、Release 清理扫描、性能稳定性、安装/升级记录）未齐全 |

---

## 3. 两车道计划与所有权

| 车道 | 计划 | 执行者能力 | 独占范围 |
| --- | --- | --- | --- |
| V-LANE | [视觉前端计划](superpowers/plans/2026-08-15-v2-5-visual-frontend.md) | 具备视觉理解和 Android Compose 能力 | `frontend/Front/app/src/main/**`、主题、可见资源、UI 测试与截图 |
| NV-LANE | [非视觉平台计划](superpowers/plans/2026-08-15-v2-5-nonvisual-platform.md) | 强编码/契约/数据库能力，不要求视觉 | Architecture 转正、`main/**`、迁移、AI 资产、Android `domain/v25`/`data/remote/v25`、Release 配置 |

共同约束：

1. 并行开发仍需使用隔离工作树/分支；前端与后端最终统一回本仓库 `main`，不在同一工作树并发编辑。
2. `ui/AppViewModel.kt` 归视觉 Agent；非视觉 Agent 不修改它。
3. `domain/v25/**` 归非视觉 Agent；接口修改需通知视觉 Agent，禁止视觉侧复制 DTO。
4. PRD、Architecture 和本 Progress 由主集成者维护；执行 Agent 不自行改写需求或宣布总版本完成。
5. 现有用户脏改动必须保留；特别是 `frontend/Front/app/build.gradle.kts` 与各目录现有本地配置。

---

## 4. 依赖图

```text
已确认 PRD + 目标 Architecture
        │
        ├──────────────→ V-01 视觉基础 ─→ V-02~V-07 ─┐
        │                                             │
        └→ NV-00 domain/v25 桥接 ─→ NV-01 契约/迁移 ─┤
                                   ├→ NV-02 项目资料  │
                                   ├→ NV-03~04 制卡   ├→ V-08/NV-08 联合集成
                                   ├→ NV-05 卡片      │       │
                                   ├→ NV-06 学习统计  │       ▼
                                   └→ NV-07 data/构建 ┘   Release 主链路
                                                               │
                                                               ▼
                                                       签名 APK + 真机证据
```

- V-01 可与 NV-00/NV-01 并行。
- V-02～V-07 可先完成 UI state 和 Preview，但真实接口验收需等待对应 NV 包。
- NV-07 可在视觉完成前完成 data 和构建配置；签名 APK 总验必须等待两车道合入。

---

## 5. NV-LANE 状态

| 包 | 状态 | 依赖 | DONE 证据 |
| --- | --- | --- | --- |
| NV-00 Android `domain/v25` 桥接 | `DONE` | 目标 Architecture | `domain/v25/V25Models.kt` / `V25Repository.kt` 接口与模型落地；单栈收敛后由 `data/remote/v25/RemoteV25Repository` 实现（commit 32052e9） |
| NV-01 契约原子转正与迁移 | `DONE` | 目标 Architecture | 三契约自标 v2.5（openapi 2.5.0）；迁移 `0f8b9f33b769_v2_5_contract`、`30364748ec32`、`88f2e1abc6f3`、`f7a2b3c4d5e6` 已落；contract 守卫套件通过（2026-08-31 全量 pytest） |
| NV-02 账号偏好与学习项目 | `DONE` | NV-01 | `/auth/*`、`/preferences`、`/projects/*` 路由与服务齐全；跨用户隔离与文件失败回滚入 integration/acceptance 套件并通过（2026-08-31） |
| NV-03 任务状态机与整批发布 | `DONE` | NV-01/NV-02 | 七态迁移与 STAGED 隔离/整批发布落地（structure-contract 4.1）；样卡持久化、0 卡失败 `TASK_ZERO_CARDS`、retry 关联均有测试（2026-08-31 通过；R25-08 已关闭） |
| NV-04 AI 资产与质量 | `DONE` | NV-03 | `agent_evolution/` prompts v6（难度锚定重设计 V25-D-33：认知动作操作化/枚举拆分/DEEP 场景化/近失示例）/ planner-output v4 / rubric v3 + manifest 守卫；密度制编排（quota 区间 + `GENERATION_SPEC`/`USER_REQUIREMENTS` 双区块 + `coverage_tier` 落库）+ 质量观测体系（`scripts/task_quality_report.py`、`Architecture/generation-quality-metrics.md`）；两轮真实验收 `435598b1`（v5）与 `3b83fb78`（v6，难度维 1.83→2.44/2.78）+ 双裁判盲评对比已登记 |
| NV-05 撤销与 AI 重写 | `DONE` | NV-01/NV-02 | 删除批次 10 秒服务端撤销窗口、重写预览两阶段 apply/cancel；`card_deletion_batches` / `card_rewrite_previews` 表与路由落地，测试通过 |
| NV-06 今日计划与统计 | `DONE` | NV-01/NV-02/NV-05 | `/study/plan`、`/study/today`、`/study/today/backlog`、`/stats/dashboard`（账号时区分桶）；时区/去重/排序测试通过 |
| NV-07 Android data 与 Release 配置 | `DONE` | NV-00 + 对应 API 稳定 | offline-foundation-v1 设备验收关闭（2bbd080）；`shanka-v25.db` Room 投影 + 评分 outbox；Release URL 编译期固定；`frontend/scripts/build-release.sh` 原子输出 APK（2.5.0 + SHA-256） |
| NV-08 平台回归与交付证据 | `TODO` | NV-02～NV-07 + V-LANE 合入 | 后端四工具、迁移演练、30 分钟稳定性、安装/升级证据未执行登记 |

非视觉计划采用 Superpowers 风险分级执行：NV-00～NV-08 分解为 Task 1～15；只有契约/迁移原子转正与
整批发布使用完整修复复审闭环，其余任务使用轻量验证或一次合并审查。前端与后端现在同属一个 Git
仓库，分别维护目录级所有权，最终从统一 `main` 生成 Release。

---

## 6. V-LANE 状态

> 视觉实现代码已大量落地（约 30 个屏幕文件、design-system 应用），但下表按状态规则要求逐包
> 登记验收证据后方可翻 `DONE`；当前证据不足，保持 `TODO` 待登记。

| 包 | 状态 | 依赖 | DONE 证据 |
| --- | --- | --- | --- |
| V-01 视觉基础与 Moods | `TODO` | 目标 Architecture | Preview 矩阵、浅深色/大字体、12 个本地头像、编译 —— 证据未登记 |
| V-02 首页/项目入口/个人主页 | `TODO` | NV-00 类型；真实验收依赖 NV-02/NV-06 | UI state tests + 真数据截图 —— 部分截图在 `releases/visual-evidence/`，验收未逐项登记 |
| V-03 项目与制卡流程 | `TODO` | NV-00；真实验收依赖 NV-02～04 | 中断恢复/样卡/生成/删除各状态截图和 UI tests —— 证据未登记 |
| V-04 牌组/卡片/撤销/重写 | `TODO` | NV-00；真实验收依赖 NV-05 | 编辑、预览替换、重启撤销、删除确认真机证据 —— 证据未登记 |
| V-05 学习/复习/自由刷题 | `TODO` | NV-00；真实验收依赖 NV-06 | 四档评级、积压、筛选、不改排程验收 —— 证据未登记 |
| V-06 统计与设置 | `TODO` | NV-00；真实验收依赖 NV-02/NV-06 | 真实/空/失败数据、时区确认、无伪 0% —— 部分截图在 `releases/visual-evidence/`，验收未逐项登记 |
| V-07 Release 视觉清理 | `TODO` | V-01～V-06 | Mock/Debug/死入口扫描、截图、UI tests、1,000 卡滚动 —— 未执行 |
| V-08 真实接口与视觉总验 | `TODO` | V-02～V-07 + NV-02～NV-07 | 全新账号主链路、浅深色/大字体、目标设备截图 —— 未执行 |

---

## 7. 联合集成与发布闸门

| 闸门 | 状态 | 通过条件 |
| --- | --- | --- |
| G1 契约稳定 | `DONE` | NV-01 完成：三契约自标 v2.5，23 项契约漂移关闭（commit 32052e9），contract 守卫套件通过 |
| G2 模块真数据 | `TODO` | 六产品模块均通过真实后端成功/空/失败/恢复状态 |
| G3 Release 清理 | `TODO` | 无 Mock、内置演示、Debug/测试入口、服务器编辑、死按钮或占位页 |
| G4 性能稳定性 | `TODO` | PRD 指定数据基线、P95 时间和 30 分钟稳定性通过 |
| G5 APK 证据 | `TODO` | `releases/app-release.apk` 版本 2.5.0、签名、SHA-256 已产出；安装/升级和 Git 状态证据未登记齐全 |
| G6 V2.5 发布 | `TODO` | 两车道 DONE 且 G1～G5 全部通过，无未声明 P0 失败 |

---

## 8. 已知风险与处理边界

| ID | 状态 | 风险 | 处理 |
| --- | --- | --- | --- |
| R25-01 | `CLOSED` | 前端曾是 nested Git，导致前后端主线分叉 | 已将前端 `main`（`ff37935`）以保留历史的 subtree 合入统一仓库 `main`（`9cc5988`）；后续仅维护统一主线 |
| R25-02 | `RESOLVED` | 当前机器契约仍为 V2.4，而 PRD/目标 Architecture 已为 V2.5 | NV-01 已完成：三契约自标 v2.5 实现事实，迁移 `0f8b9f33b769_v2_5_contract` 等落地，contract 守卫通过（2026-08-31 全量 pytest） |
| R25-03 | `RESOLVED` | 当前生成路径可能在任务完成前写入普通 Cards | STAGED/PUBLISHED 与统一可见谓词已实现（structure-contract 3.9/4.1），失败任务全隔离，integration 套件覆盖 |
| R25-04 | `RESOLVED` | 当前难度校验要求三档均大于 0，且仍使用 APPLICATION | 已迁移为 10% 整数比例（允许 0、不可全 0）与 `DEEP_QUESTION` 枚举（openapi `DifficultyRatio`）；非法配置创建/修改时即拒绝 |
| R25-05 | `RESOLVED` | 当前任务存在 PAUSED/resume/cancel，与 V2.5 用户状态冲突 | 用户侧 PAUSED/resume/cancel API 已删除（openapi 无对应路径）；内部恢复走租约/心跳重新抢占；历史 PAUSED 迁为 FAILED(LEGACY_PAUSED_TASK) |
| R25-06 | `RESOLVED` | 当前 `AppViewModel` 混合运行时 Mock、JSONObject 和网络编排 | 单栈收敛（commit 32052e9）：统一 Retrofit/OkHttp NetworkStack + Room 投影 + 评分 outbox，AppViewModel 网络编排移除；设备验收关闭（2bbd080）；Release 零 Mock 终验归 V-07/G3 |
| R25-07 | `RESOLVED` | SQLite 单写者下生成长事务可能阻塞撤销/设置写入 | 设计缓解（LLM 调用移出写事务、租约/短事务发布）原有 service 级验证；2026-09-04 补 HTTP 端到端并发压力测试 `test_http_write_pressure.py`（同用户 GENERATING 任务批次写推进 + 3 线程 × 20 轮评分/偏好/牌组混合写并发，全 2xx 零锁死、任务收敛 COMPLETED、删除撤销路径复验），入 CI 可复跑。真机 30 分钟长稳仍归 NV-08/G4 发布闸门 |
| R25-08 | `RESOLVED` | `test_operation_key_task_domain_and_ledger_idempotent` 断言未覆盖 V2.5 持久化样卡的 `sample:` 账本前缀 | 2026-08-31 密度制批次中更新断言纳入 `sample:` 前缀并按新配额校正计数；全量 pytest 868/868 通过 |
| R25-10 | `RESOLVED`（后端/契约侧；前端接入待办） | 资料删除确认页未展示“将影响的卡片数量”（PRD V25-GEN-FR-02 要求展示数量；Material 级删除无 preflight 端点，前端仅给保留/一并删除两选项文案） | 2026-09-04 新增 `GET /projects/{id}/materials/{mid}/deletion-preflight`：`impact.affected_card_count` 按统一可见谓词计数（STAGED/删除批次排除）、`silently_cancelled_task_count` 展示静默取消任务数（V25-D-30 无 blocker 语义，`can_delete` 恒 true）；集成测试 `test_projects_material_deletion_preflight_counts_and_silent_cancel`（计数口径 + 跨用户/不存在 404）；openapi.yaml 路径条目与 structure-contract 6.2 端点表同步登记。**待办**：App 尚未调用该端点（`MaterialManagementScreen` 仍为两选项文案，注释称端点不存在已过时），确认页展示影响数量后 PRD V25-GEN-FR-02 才算用户可见达成 |
| R25-09 | `OPEN` | alembic 对运行库操作必须显式指定 URL，否则回落 alembic.ini 占位库 | 2026-08-31 事故：`coverage_tier` 迁移未应用到 `main/data/shanka.db`（运行库），代码写新列致规划阶段 flush 失败、任务 30 分钟空转重试循环；已用 `DATABASE_URL=sqlite:////…/main/data/shanka.db alembic upgrade head` 补齐并自愈。纪律：对运行库执行迁移/检查一律显式带 URL，不以 cwd 相对路径兜底 |
| R25-11 | `RESOLVED` | 服务端延迟无 SLO 基线与回归证据（G4 前置缺失）；三个 duration histogram 全用默认桶（上限 10s），生成任务耗时会整体落 +Inf 致 p99 失真；`http_requests_total` 以原始路径为 label 存在高基数风险；测试基座无覆盖率/无超时保护 | 2026-09-05 测试地基工作包：structure-contract 8.3 桶边界与 path 归一化契约化（LLM 桶至 300s、任务桶至 1800s；动态路径记路由模板、未匹配记 `unmatched`）+ 新增 8.6 服务端延迟基线（读 p95≤500ms / 写 p95≤800ms 初始参考值 + 校准纪律 + 首轮实测快照全达标：dashboard p95≈144ms 最重，写 12.2ms）；守卫测试 4 项入 `test_metrics.py`（histogram 语义 / 模板归一化 / unmatched 有界 / 定制桶）；`test_api_latency_baseline.py` 千卡万事件 5 端点采样入 CI；pytest-timeout（单用例 300s）+ perf/pressure markers + pytest-cov 覆盖率报告（CI artifact，基线先行不设门槛：2026-09-05 首轮全量 878/878 绿，总覆盖率 91.1%）；study 三端点 HTTP 测试补齐。30 分钟真机长稳仍归 NV-08/G4 |

风险关闭时保留原记录，改为 `RESOLVED` 并附提交/测试证据，不删除历史。

---

## 9. Progress 更新纪律

只有主集成者可以更新状态：

1. Agent 报告完成后读取实际 diff、提交和测试输出；
2. 验证文件所有权和用户脏改动未被覆盖；
3. 复跑与风险相称的关键测试；
4. 写入 commit、测试计数、APK hash 或设备报告；
5. 再把对应包从 TODO/DOING 改为 DONE。

若只有计划、代码片段、Mock、失败测试或未安装 APK，状态保持 TODO/DOING。
