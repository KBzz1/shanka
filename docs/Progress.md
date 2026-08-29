# 闪卡 App V2.5 执行地图

本文是 V2.5 唯一进度、依赖与 DONE 事实源。需求权威为
[V2.5 PRD](PRD/V2.5/prd_v2_5.md)，目标技术设计为
[V2.5 Architecture](Architecture/v2.5-target-architecture.md)。两份实施计划只负责细化任务，不得各自另建总状态表。

最后事实审计：2026-08-15。

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
| V2.5 目标 Architecture | `DONE` | 聚合、资源、状态机、API、迁移、双 Agent 边界和 Release 设计已写入 `Architecture/v2.5-target-architecture.md` |
| 当前机器契约 | `DONE`（仅 V2.4 实现事实） | `structure-contract/openapi/database-design` 继续如实描述现行实现；尚未转成 V2.5 |
| V2.5 后端/数据库实现 | `TODO` | 不能从目标 Architecture 推断已实现 |
| V2.5 Android 视觉实现 | `TODO` | 当前仍存在运行时 Mock/Debug 及未完成流程，需视觉计划收敛 |
| V2.5 Android data/Release | `TODO` | DTO、V2.5 Repository、固定 Release 环境和正式 APK 尚未验收 |
| V2.5 正式发布 | `TODO` | 尚无满足 V2.5 全链路、性能和真机要求的签名 APK 证据 |

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
| NV-00 Android `domain/v25` 桥接 | `TODO` | 目标 Architecture | 接口/模型 contract test + frontend commit |
| NV-01 契约原子转正与迁移 | `TODO` | 目标 Architecture | Schema/OpenAPI/ORM/DB 守卫、空库与 V2.4 副本升级、mypy/ruff/pytest |
| NV-02 账号偏好与学习项目 | `TODO` | NV-01 | API/integration/acceptance、跨用户隔离、文件失败回滚 |
| NV-03 任务状态机与整批发布 | `TODO` | NV-01/NV-02 | 七态迁移、样卡恢复、0 卡失败、STAGED 不可见、重试关联 |
| NV-04 AI 资产与质量 | `TODO` | NV-03 | 新版本资产、manifest 守卫、三覆盖模式质量记录 |
| NV-05 撤销与 AI 重写 | `TODO` | NV-01/NV-02 | 10 秒服务端窗口、重启恢复、finalizer 幂等、rewrite CAS |
| NV-06 今日计划与统计 | `TODO` | NV-01/NV-02/NV-05 | 时区/DST/去重/排序/独立牌组测试、基线数据性能 |
| NV-07 Android data 与 Release 配置 | `TODO` | NV-00 + 对应 API 稳定 | Repository contract、test/assembleDebug、固定正式 URL、原子 APK 输出脚本 |
| NV-08 平台回归与交付证据 | `TODO` | NV-02～NV-07 + V-LANE 合入 | 后端四工具、迁移、质量、签名/哈希/安装/30 分钟稳定性 |

非视觉计划采用 Superpowers 风险分级执行：NV-00～NV-08 分解为 Task 1～15；只有契约/迁移原子转正与
整批发布使用完整修复复审闭环，其余任务使用轻量验证或一次合并审查。前端与后端现在同属一个 Git
仓库，分别维护目录级所有权，最终从统一 `main` 生成 Release。

---

## 6. V-LANE 状态

| 包 | 状态 | 依赖 | DONE 证据 |
| --- | --- | --- | --- |
| V-01 视觉基础与 Moods | `TODO` | 目标 Architecture | Preview 矩阵、浅深色/大字体、12 个本地头像、编译 |
| V-02 首页/项目入口/个人主页 | `TODO` | NV-00 类型；真实验收依赖 NV-02/NV-06 | UI state tests + 真数据截图 |
| V-03 项目与制卡流程 | `TODO` | NV-00；真实验收依赖 NV-02～04 | 中断恢复/样卡/生成/删除各状态截图和 UI tests |
| V-04 牌组/卡片/撤销/重写 | `TODO` | NV-00；真实验收依赖 NV-05 | 编辑、预览替换、重启撤销、删除确认真机证据 |
| V-05 学习/复习/自由刷题 | `TODO` | NV-00；真实验收依赖 NV-06 | 四档评级、积压、筛选、不改排程验收 |
| V-06 统计与设置 | `TODO` | NV-00；真实验收依赖 NV-02/NV-06 | 真实/空/失败数据、时区确认、无伪 0% |
| V-07 Release 视觉清理 | `TODO` | V-01～V-06 | Mock/Debug/死入口扫描、截图、UI tests、1,000 卡滚动 |
| V-08 真实接口与视觉总验 | `TODO` | V-02～V-07 + NV-02～NV-07 | 全新账号主链路、浅深色/大字体、目标设备截图 |

---

## 7. 联合集成与发布闸门

| 闸门 | 状态 | 通过条件 |
| --- | --- | --- |
| G1 契约稳定 | `TODO` | NV-01 完成，视觉 Agent 消费的字段不再临时变化 |
| G2 模块真数据 | `TODO` | 六产品模块均通过真实后端成功/空/失败/恢复状态 |
| G3 Release 清理 | `TODO` | 无 Mock、内置演示、Debug/测试入口、服务器编辑、死按钮或占位页 |
| G4 性能稳定性 | `TODO` | PRD 指定数据基线、P95 时间和 30 分钟稳定性通过 |
| G5 APK 证据 | `TODO` | `releases/app-release.apk` 版本 2.5.0、签名、SHA-256、安装/升级和 Git 状态齐全 |
| G6 V2.5 发布 | `TODO` | 两车道 DONE 且 G1～G5 全部通过，无未声明 P0 失败 |

---

## 8. 已知风险与处理边界

| ID | 状态 | 风险 | 处理 |
| --- | --- | --- | --- |
| R25-01 | `CLOSED` | 前端曾是 nested Git，导致前后端主线分叉 | 已将前端 `main`（`ff37935`）以保留历史的 subtree 合入统一仓库 `main`（`9cc5988`）；后续仅维护统一主线 |
| R25-02 | `ACCEPTED` | 当前机器契约仍为 V2.4，而 PRD/目标 Architecture 已为 V2.5 | NV-01 一次性同步 Schema/OpenAPI/ORM/迁移；在此之前目标字段不得称已部署 |
| R25-03 | `OPEN` | 当前生成路径可能在任务完成前写入普通 Cards | NV-03 引入 STAGED/PUBLISHED 和统一可见谓词，失败任务全隔离 |
| R25-04 | `OPEN` | 当前难度校验要求三档均大于 0，且仍使用 APPLICATION | NV-01/NV-03 迁移为允许 0 的整数比例和 DEEP_QUESTION |
| R25-05 | `OPEN` | 当前任务存在 PAUSED/resume/cancel，与 V2.5 用户状态冲突 | NV-01 迁移旧状态；NV-03 移除用户 API，内部恢复使用租约 |
| R25-06 | `OPEN` | 当前 `AppViewModel` 混合运行时 Mock、JSONObject 和网络编排 | NV-00 提供桥接；视觉 Agent 重构 UI state；V-07 验证 Release 零 Mock |
| R25-07 | `OPEN` | SQLite 单写者下生成长事务可能阻塞撤销/设置写入 | NV-03 将 LLM 调用移出写事务，事务只包短状态/发布更新；NV-08 压测 |

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
