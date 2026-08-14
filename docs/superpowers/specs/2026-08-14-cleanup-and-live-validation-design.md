# 清理收尾与补做验证 + 前端整合：设计规格

- 日期：2026-08-14
- 状态：用户已确认设计（脑暴流程四节全部确认）
- 上游权威：本文件为新工作包的设计规格；涉及契约变更的部分按契约驱动流程同步 PRD/Architecture

## 0. 背景与决策记录

前序长程任务包（shanka-llm-account-long-run-v1）已 GOAL_DONE：账号体系落地（P2–P8），
旧 device_id 数据按当时决策 D-06「不迁不删、无访问路径」保留。本工作包执行四项用户新决策：

1. **决策翻转 D-06→V2.3（2026-08-14）**：旧设备数据连同旧架构**全部物理删除**——
   devices 表、8 张 owner 表的 device_id 列、遗留约束/索引、代码与契约中的设备残留；
   删除不可逆（downgrade 显式拒绝）；本机开发库直接迁移（不备份）；PRD 升 V2.3 记录。
2. **毛刺修复**：三类 9 条（见 §2）。
3. **补做三件未运行验证**：真实后端联调（quick/full）、live 真实 LLM（成本上限 ¥3）、
   Android 真机测试（设备 adc60f1a 已连接）。
4. **前端整合**：上游 JIANGYOU3/Shanka（origin）新登录界面视觉 + 本仓库 P6 后端对接
   架构合并；整合完成后 fork→push→PR（push 前再向用户确认）。

## 1. 设备架构彻底清除（PRD V2.3）

### 1.1 数据库迁移

- 新 revision（`down_revision` = 当前 head `e85c78b2a345`，文件名由 alembic revision 生成）：
  - `op.drop_table("devices")`
  - 8 张表（pdf_files/tasks/decks/cards/review_events/llm_call_attempts/api_keys/
    idempotency_keys）经 `batch_alter_table`：删除 `device_id` 列、删除
    `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`、删除遗留唯一约束
    （idempotency_keys 的 `uq_idempotency_keys_device_path`、api_keys 的
    `uq_api_keys_device_id`、review_events 的 `uq_review_events_device_client`——
    P3 保留的全部三个 device 版唯一约束）与相关索引。
  - 沿用 env.py 迁移连接层 FK 关闭机制（batch 重建 FK 父表防级联清空——P3 已验证）。
- **downgrade 语义**：函数第一行 `raise RuntimeError("V2.3 起设备数据已物理删除，迁移不可逆；回退请恢复升级前备份")`——延续 fail-closed 精神，不假装可回滚。
- 验证：空库 upgrade 全链（7 revisions）+ `alembic check` 零漂移；开发库
  `main/shanka.db` 直接 `alembic upgrade head`（旧数据随列删除消失，用户已确认不备份）。

### 1.2 契约同步

- 新建 `docs/PRD/V2.3/prd_v2_3.md`：继承 V2.2，变更清单记 D-06 撤销（物理清除决策）、
  结构变更（devices 表删除、device_id 列删除）、排除项（不回滚）。
- `docs/Architecture/database-design.md`：删除 2.1 devices 表节、8 表 device_id 行与
  CHECK/遗留约束行、§0/§1 ER 图设备残留、§7.1 更新为 V2.3 落地记录。
- `docs/Architecture/structure-contract.md`：§9 对照表指向 V2.3；全文 grep 设备残留句
  清理（历史决策描述句除外，标注「V2.1 历史」）。
- 红线 2 守卫（ORM↔database-design）每个提交内全绿。

### 1.3 代码清理

- `main/infra/db/models.py`：Device 模型删除；8 表 device_id 列、FK、约束、索引声明删除；
  IdempotencyKey `allow_partial_pks` **移除**（V2.3 起 user_id 恒非空，NULL 主键语义
  残留随之消除——P3 过渡注释一并清理）；ApiKey 过渡注释清理。
- 全仓 grep `device_id` 运行时引用清零：services/app/handlers 谓词与参数（P4 已切换完毕，
  残留应仅 models 与迁移——逐一核实）；tests 中 device 域种子/断言按新结构重写；
  legacy fixture（P3 迁移测试的 2a391e994f93 旧库副本 SQL 直插）——迁移测试中「旧行保留」
  类断言**随语义翻转删除或改写**（V2.3 后升级链上旧结构仍会被建立再删除——测试改为断言
  升级到 V2.3 后 device 列不存在）。
- `docs/Progress.md` ACC-P3 条目中 D-06 相关表述按 V2.3 加注（历史记录不改写，追加决策行）。

### 1.4 决策翻转的连带项

- P3 的 fail-closed downgrade（a7cc699f3fd8/e85c78b2a345 的 `_fail_closed_check`）保留
  原样（历史 revision 不改）；V2.3 起删除不可逆取代 fail-closed。
- test_alembic_migration.py：旧库副本往返测试中「device 域行保留」断言随结构删除改写；
  ddc6 层 CI 断言（降 2a391e994f93 行数守恒）——该层仍在迁移链上，保留其语义但行集不含
  device 列断言。

## 2. 毛刺修复（9 条）

1. test-platform `scenarios/baseline/api_smoke.py`：响应解析加 isinstance 守卫
   （网关 502/HTML 响应时干净 FAIL 步骤而非 AttributeError 崩出 traceback）。
2. `scenarios/flow/live_flow.py`：观测账号 bootstrap 失败路径补 WARN（会话可能未撤销）。
3. `live_flow.py` `.env` 绝对路径改 `__file__` 相对推导仓库根。
4. 平台测试补：幂等键/client_event_id 跨用户复用场景（DESIGN 8.2 缺口——不同用户同 key
   同 body 各自成功、互不重放）。
5. `main/tests/live/driver.py`：report/args 的 device_id 遗留字段移除（随 §1 设备清理）。
6. `main/app/middleware/rate_limit.py`：write 桶 60s 窗口 clock 注入（与 ip_limit.py 同款
   透传，测试固定时钟消 flakiness）。
7. Android `FlashcardsAppTest.storedSessionEntersTheMainScreen`：引入注入缝
   （AppViewModel 可注入 baseUrl/sessionStore 或等价最小方案），测试不依赖「后端未启动」。
8. Android `AuthViewModel.logout()`：改先本地登出（立即回登录页）+ 后台撤销服务器 token
   （fire-and-forget），断网时退出不再阻塞。
9. 文档口径：SDD 报告计数/复跑次数口径、过期注释（P4/P6 已登记清单）统一清理。

## 3. 补做三件未运行验证

### 3.1 真实后端联调（test-platform quick/full）

- 前置：开发库迁移 V2.3 → 启动本地后端（uvicorn，端口 8000）。
- 执行：`runner quick`（auth/isolation/api_smoke 等非 LLM 场景）、`runner full`
  （含 generation 观察场景但不触发真实 LLM 的路径）；凭据从 env 提供测试账号。
- 证据：真实 HTTP 请求/响应与场景 FAIL=0 记录进报告（不落 repo 的敏感项除外）。

### 3.2 live 真实 LLM（成本上限 ¥3）

- `.env` 加载真实 DEEPSEEK_API_KEY（用户已授权）；`runner live --confirm-cost`
  （成本闸门最坏推导 ¥1.86 ≤ ¥3 上限）；运行后批次对账实际 attempts/token/成本。
- 触发条件：成本闸门放行 + Key 可用；若实际成本接近 ¥3 即停（闸门即停逻辑）。
- 未触发/未运行的任何部分如实声明。

### 3.3 Android 真机

- 设备：`adc60f1a`（adb 已确认在线）。
- `adb reverse tcp:8000 tcp:8000` 端口反向（真机访问本机后端）。
- `./gradlew connectedDebugAndroidTest`：BackendClientInstrumentedTest +
  FlashcardsAppTest 真机运行；与 3.1 的后端联调配合（登录态测试需真实后端）。
- 上游整合（§4）完成后对整合结果跑同一验收。

## 4. 前端整合（上游视觉 + 本仓库架构）

### 4.1 现状

- 本仓库 frontend-app origin = JIANGYOU3/Shanka；本地 main 含 P6 的 4 commits
  （60d62a3..f15457b），基于 2a9f6b7。
- 上游最新 `ef2ed95`（「系统化管理字体，制作了登录界面」）：127 files +25669/-6581——
  AuthScreen.kt（687 行登录界面）、字体系统（FONT_LIBRARY/DeckTheme）、Screens.kt 拆分为
  多个 Screen 文件、AppTheme/AppViewModel 大改。

### 4.2 整合规则

- **视觉/UI 层 → 上游为准**：AuthScreen 及其设计系统（字体/主题/组件）一律采用上游版本。
- **后端对接层 → 本仓库 P6 实现为准**：SessionStore（Keystore 加密）、BackendClient
  （Bearer 四端点/401 语义/显式不带头）、AuthViewModel 状态机（checkSession 三分支/
  业务 401 清会话/网络失败不退出）、40 个 JVM 测试全部保留。
- **接线**：上游 AuthScreen 的登录/注册/退出触发点接到本仓库 repository 调用；上游
  UI 状态（loading/错误展示）对接本仓库 ViewModel 状态；错误文案映射保持本仓库口径
  （INVALID_CREDENTIALS/USERNAME_TAKEN/RATE_LIMITED/网络错误）。
- **冲突解决顺序**：merge origin/main → UI 冲突取上游、逻辑冲突取本仓库；上游
  AppViewModel 与本地 AuthViewModel 的职责切分以「视觉归上游组件、状态归本仓库状态机」
  为界，必要时保留两个 ViewModel（UI 层上、状态层下）。

### 4.3 整合验收

- 本仓库 40 个 JVM 测试全绿（适配上游组件签名后的必要调整）；上游自带测试（若有）同步
  跑通；assembleDebug + 3.3 真机验收。
- 敏感纪律不变：密码不持久化、Authorization 不进日志。

### 4.4 fork/push/PR 流程

- 整合完成并全绿后：GitHub 创建 fork → 本地加 fork remote → push 分支 → 从 fork 提 PR
  到 JIANGYOU3/Shanka。
- **push 与 PR 属外发动作，执行前再次向用户确认**。

## 5. 全局约束（沿用）

1. 解释器 `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`；后端工作目录
   `main/`；平台 `test-platform/`；Android `frontend-app/Front/`。
2. 四工具全绿（main/ pytest/ruff/format/mypy）+ test-platform pytest 全绿 + gradle
   test/assembleDebug 全绿——每个提交内相关子集绿。
3. TDD：能先红的先红；SDD 流程（implementer + reviewer 双审）。
4. 敏感信息不进日志/报告/命令参数；live 预算 ¥3 硬上限。
5. 无破坏性 git；不 push 不 PR（§4.4 除外且需确认）；不碰
   docs/llm-account-long-run-v1/ 与 docs/account-auth-test-platform-long-run-v1/ 的
   历史记录（追加 V2.3 决策到 PRD/Progress 属允许范围）。

## 6. 验收总览

- [ ] V2.3 迁移：空库 7 revisions 全链 + check 零漂移 + 开发库迁移成功 + 全仓 device_id
      运行时引用清零
- [ ] 契约三处一致（PRD V2.3/database-design/structure-contract）+ 守卫全绿
- [ ] 毛刺 9 条闭环（各自带测试或证据）
- [ ] 联调：quick/full 对真实后端跑通（FAIL=0）
- [ ] live：真实 LLM 全链 + 成本 ≤ ¥3 + 对账记录
- [ ] 真机：connectedDebugAndroidTest 通过（整合后版本）
- [ ] 前端整合：上游视觉 + 本仓库逻辑，40/40 测试 + assembleDebug
- [ ] 全量回归：main/ 四工具 + test-platform + Android 三面全绿
