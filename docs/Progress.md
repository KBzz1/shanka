# 闪卡 App V2.5 执行地图

本文是 V2.5 唯一进度、依赖与 DONE 事实源。需求权威为
[V2.5 PRD](PRD/V2.5/prd_v2_5.md)，目标技术设计为
[V2.5 Architecture](Architecture/v2.5-target-architecture.md)（已转正，现行契约见
`Architecture/structure-contract.md` / `openapi.yaml` / `database-design.md`）。
两份实施计划只负责细化任务，不得各自另建总状态表。

最后事实审计：2026-08-31（第二次）。审计手段：全量后端测试复跑（`conda run -n shanka-backend python -m pytest`：
**868 通过 / 0 失败 / 868 项**，R25-08 已关闭）+ 密度制真实验收（任务 `435598b1`：18 卡落在
[12,25] 区间、COMPLETED、评分覆盖 100%；观测与双裁判盲评报告见
`Architecture/generation-quality-metrics.md`）+ 提交/契约/迁移证据核对
（32052e9 单栈收敛与 23 项契约漂移关闭、2bbd080 offline-foundation-v1 设备验收、29cf2d5 文档刷新）。

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
| 当前机器契约 | `DONE`（V2.5 实现事实） | `structure-contract.md`（v2.5）、`openapi.yaml`（2.5.0）、`database-design.md`（v2.5）均自标 V2.5 实现事实，contract 守卫套件通过 |
| V2.5 后端/数据库实现 | `DONE` | V2.5 契约迁移已落（`0f8b9f33b769_v2_5_contract` 等共 15 个迁移，含 `a3f8d21c9e47` coverage_tier）；projects/study/tasks 七态/删除批次/重写预览等 V2.5 服务与路由齐全；密度制数量编排上线（V25-D-25~28，`knowledge_points.coverage_tier` 已补应用到运行库）；2026-08-31 全量 pytest 868/868 通过，真实验收证据见 `Architecture/generation-quality-metrics.md` |
| V2.5 Android 视觉实现 | `DOING` | 视觉实现已大量落地：UI 拆分为约 30 个屏幕文件、design-system 402dp 体系应用、真机截图证据在 `releases/visual-evidence/`；但 V-01~V-08 逐包验收证据（Preview 矩阵、浅深色/大字体、UI tests）未登记 |
| V2.5 Android data/Release | `DONE` | offline-foundation-v1 设备验收关闭（commit 2bbd080）：统一 NetworkStack + Room 投影 `shanka-v25.db` + 评分 outbox（`docs/frontend/offline-data-layer.md`）；debug 与正式包安装隔离（commit 1d175a6）；Release 编译期固定 `https://shanka.kbzz1.top`；Release APK 2.5.0 + SHA-256 已产出（`releases/`） |
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
| NV-04 AI 资产与质量 | `DONE` | NV-03 | `agent_evolution/` prompts v5 / planner-output v4 / rubrics v3 + manifest 守卫；密度制编排（quota 区间 + `GENERATION_SPEC`/`USER_REQUIREMENTS` 双区块 + `coverage_tier` 落库）+ 质量观测体系（`scripts/task_quality_report.py`、`Architecture/generation-quality-metrics.md`）；首轮真实验收 `435598b1`（18 卡 [12,25]、COMPLETED、覆盖 100%）+ 双裁判盲评对比已登记 |
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
| R25-07 | `OPEN` | SQLite 单写者下生成长事务可能阻塞撤销/设置写入 | LLM 调用已移出写事务（租约/短事务发布），但 NV-08 压测未执行，证据未登记 |
| R25-08 | `RESOLVED` | `test_operation_key_task_domain_and_ledger_idempotent` 断言未覆盖 V2.5 持久化样卡的 `sample:` 账本前缀 | 2026-08-31 密度制批次中更新断言纳入 `sample:` 前缀并按新配额校正计数；全量 pytest 868/868 通过 |
| R25-09 | `OPEN` | alembic 对运行库操作必须显式指定 URL，否则回落 alembic.ini 占位库 | 2026-08-31 事故：`coverage_tier` 迁移未应用到 `main/data/shanka.db`（运行库），代码写新列致规划阶段 flush 失败、任务 30 分钟空转重试循环；已用 `DATABASE_URL=sqlite:////…/main/data/shanka.db alembic upgrade head` 补齐并自愈。纪律：对运行库执行迁移/检查一律显式带 URL，不以 cwd 相对路径兜底 |

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
