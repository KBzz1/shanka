# 任务地图（进度跟踪）

> **使用说明**：TDD 细节、失败测试代码、实现要点与提交信息以
> `docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md`（LLM 段）与
> `docs/account-auth-test-platform-long-run-v1/DESIGN.md`（账号段）为唯一权威。本文件只做概要导航
> 与勾选跟踪；**勾选不等于完成**，完成以真实命令证据（pytest 失败→通过、四工具、迁移检查、canary）
> 为准。执行时每个任务用 superpowers 技能 `subagent-driven-development`（独立 subagent 逐任务实现，
> 主 Worker 集成与验收）完成。

## 第 1 阶段：LLM 链路升级（17 任务，P1）

设计：`docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md`
步骤：`docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md`

| # | 任务 | 关键产物 | 提交信息 | 完成门禁 |
| --- | --- | --- | --- | --- |
| 1 | 迁移 0003 + ORM | TextChunk/LlmCallAttempt 新表 + KnowledgePoint/Batch/Task 加列；`0003_llm_pipeline_upgrade.py` | `feat(llm-upgrade): 迁移 0003——text_chunks/llm_call_attempts 新表 + 三表加列` | 空库 upgrade→downgrade→upgrade 往返；`alembic check` 零漂移 |
| 2 | Settings 9 个硬上限/预算字段 | `app/config.py` 9 字段 | `feat(llm-upgrade): Settings 硬上限与预算字段` | test_config_defaults 通过 |
| 3 | 页文本解析 + text_chunks 持久化 | `parser.extract_pages`、`text_chunks.py` 三函数、scanner 接线 | `feat(llm-upgrade): 页文本提取与 text_chunks 持久化（一页一行）` | 确定性/往返/重建级联测试通过 |
| 4 | 预算与三层配额算法 | `services/generation/quota.py`（纯函数）、planning.py 预算入口迁移 | `feat(llm-upgrade): 单元预算与三层最大余数法配额` | spec §3.5 例子确定性断言通过 |
| 5 | LLM 资产 v3/v2 + manifest + 加载扩展 | prompts/v3、rubrics/v2、schemas/v2、manifest、CHANGELOG、`prompts.py` 多入口 | `feat(llm-upgrade): 资产 v3/v2（planner/generator/rewrite/scoring）+ manifest 多入口` | 版本契约测试；`asset_versions()` 扩展键 |
| 6 | DeepSeek 适配层 retryable 区分 | `RetryableUpstreamError`；401 非重试 / 429/5xx retryable | `feat(llm-upgrade): adapter 区分 401 非重试与 429/5xx retryable` | mock transport 测试通过 |
| 7 | llm_call_attempts 账本服务层 | `services/generation/ledger.py` 全部函数 | `feat(llm-upgrade): LLM 调用账本服务层（STARTED 占位/终态/UNKNOWN/预算/恢复）` | 唯一约束冲突 → IDEMPOTENCY_CONFLICT |
| 8 | 任务创建改造 | create_task → PENDING+PLANNING、预算校验、task_view 加字段 | `feat(llm-upgrade): 任务创建改 PENDING+PLANNING + 预算上限校验` | 既有创建/幂等/删除保护测试全绿 |
| 9 | 规划执行 | planner_validator、planning_executor（CAS1/CAS2/快照冻结/复用/三分支）、executor 接线 | `feat(llm-upgrade): 规划执行（CAS 抢占/快照冻结/账本恢复/合并落库/空单元三分支）` | 6 组规划执行测试通过 |
| 10 | 生成批改造 | 批=单元、锚定校验、页文本输入、账本同事务、rubric fake 退役 | `feat(llm-upgrade): 生成批=单元（锚定校验/页文本输入/账本同事务）` | plan_batches 1 单元 1 批断言 |
| 11 | SCORING 阶段 | scoring_validator、scoring.py（确定性抽样/合批/回写守卫/非阻塞）、executor SCORING 分支 | `feat(llm-upgrade): SCORING 阶段（确定性抽样/合批/回写守卫/非阻塞）` | 5 组 scoring 测试通过 |
| 12 | quality-summary 改造 | eligible/scored/sampling_rate、difficulty 归因走单元、cost scope | `feat(llm-upgrade): quality-summary 分母/归因/成本口径修正` | 分母与 scope 断言通过 |
| 13 | 估算删除 | 删 /tasks/estimate、token_estimator、planning.py；守卫更新 | `feat(llm-upgrade): 删除 /tasks/estimate 与 token 估算链路` | 端点 404、模块不可导入 |
| 14 | 契约同步 | structure-contract（3.5/3.6/3.7/3.10/6.10/8.5）、PRD 5.4.1/5.6/5.7、database-design、openapi、守卫 | `docs: LLM 链路升级契约同步（…）` | 守卫测试全绿；红线 5 一致 |
| 15 | 前端改造 | `frontend-app/Front/` 去 estimate + 三阶段/空结果展示 | `feat(frontend): 任务创建去 estimate + PLANNING/GENERATING/SCORING 展示` | 本机构建/静态检查；无 estimate 引用 |
| 16 | V4/V5A/V6 测试更新 + 全量回归 | tasks/generation/api/acceptance 测试按新语义更新 | `test: V4/V5A/V6 测试按 LLM 链路新语义更新` | 全量 pytest + 四工具全绿 |
| 17 | canary + 完成口径 | 受控真实 canary（Planner→Generator→Scoring）、Progress 登记、spec 状态行 | `docs: LLM 链路升级完成（canary 通过，R-03 RESOLVED）` | canary 通过、预算上限内、R-03 RESOLVED |

- [x] T1 迁移 0003 + ORM
- [x] T2 Settings 9 个硬上限/预算字段
- [x] T3 页文本解析 + text_chunks 持久化
- [x] T4 预算与三层配额算法
- [x] T5 LLM 资产 v3/v2 + manifest + 加载扩展
- [x] T6 DeepSeek 适配层 retryable 区分
- [x] T7 llm_call_attempts 账本服务层
- [x] T8 任务创建改造（PENDING+PLANNING、预算校验、创建快照）
- [x] T9 规划执行（CAS 抢占/快照冻结/分组调用/合并落库）
- [x] T10 生成批改造（批=单元、锚定、页文本输入）
- [x] T11 SCORING 阶段（抽样/合批/回写守卫）
- [x] T12 quality-summary 改造（分母/归因/成本口径）
- [x] T13 估算删除（后端）
- [x] T14 契约同步（文档 + 守卫 + 错误契约）
- [x] T15 前端改造（frontend-app/Front/）
- [x] T16 V4/V5A/V6 测试更新 + 全量回归
- [x] T17 canary + 完成口径 + Progress 更新

**P1 门禁（冻结 LLM 基线后进入账号阶段）**：T1–T17 全部完成且有真实命令证据；canary 通过、
Progress 登记（LOCAL_IMPLEMENTATION_DONE → PRODUCTION_VALIDATED）、R-03 RESOLVED；记录
`LLM_BASELINE_COMMIT` 与真实 Alembic head。

## 第 2 阶段：账号登录与测试平台（P2–P8）

设计：`docs/account-auth-test-platform-long-run-v1/DESIGN.md`（本包 §4.2 导航；**§4.4/§5.3 的
legacy claim 部分不在本包范围**，用户 2026-08-13 决策）；行为规范见本包 `WORKER_PROMPT.md`「目标二」7 节。

### P2 契约 V2.2

- [x] 新建 `docs/PRD/V2.2/prd_v2_2.md`（继承 v2.1，账号会话取代 D-02；不篡改 v2.1 历史）
- [x] 原子同步 `docs/Architecture/structure-contract.md`、`docs/Architecture/openapi.yaml`（TASKS 原文写 `main/openapi.yaml`，实际权威路径为 docs/Architecture）、`docs/Architecture/database-design.md`、前端对接、错误码/文案、contract guards
- [x] 账号 HTTP 契约固定（/auth/register、/auth/login、/auth/logout、/auth/me；无 legacy claim 端点）
- [x] 更新当前 PRD 链接与 `docs/Progress.md`（只报告真实状态）

### P3 数据地基

- [x] 基于 P1 门禁记录的真实 Alembic head 创建下一 revision（不硬编码 `0004`）
- [x] users / auth_sessions 新表；owner 表加 user_id 列（新写入必填，历史 device_id 行保留不动）
- [x] 空库/副本 upgrade→downgrade→upgrade；存在新用户数据时 downgrade fail closed
- [x] `alembic check` 零漂移；计数/FK/索引守恒；PDF storage manifest 前后一致（无新增 missing/orphan）
- [x] 旧 device_id 数据不迁移/不认领/不删除（用户决策）；无 API Key 密文搬运

### P4 后端切换

- [x] register/login/logout/me 端点；Argon2id（生产参数守卫）；dummy 校验
- [x] 256-bit opaque token + SHA-256 摘要；30 天有效期；logout 只撤销当前 session
- [x] `AuthPrincipal(user_id, session_id)`；Bearer 401 + WWW-Authenticate；跨用户统一 404
- [x] 全部 owner roots 与幂等域切换 user_id；X-Device-ID 退出普通认证/授权/幂等/限流
- [x] IP + 用户名限流；敏感路径统一脱敏（服务端完成；client 脱敏随 P6 Android / P7 test-platform）

### P5 LLM 后台 user_id 接续

- [x] tasks.user_id 为后台执行身份；executor 不持有 bearer token
- [x] llm_call_attempts / API Key / quality-summary / observability 按 user_id；匿名 /metrics 无身份聚合
- [x] logout/session expiry 后任务继续；operation_key/fingerprint/CAS/配额不依赖 session
- [x] v3/v2 资产只读消费，不修改语义/版本

### P6 Android

- [x] Login/Register 状态与界面；启动 token + /auth/me 决定入口
- [x] Keystore 加密会话存储替代 SecureDeviceIdentityStore；密码不持久化
- [x] 网络层 Bearer；普通请求移除 X-Device-ID；401 清会话回登录页；网络失败不误判退出

### P7 test-platform v2

- [ ] client register/login/set_token/logout + 敏感路径脱敏；runner 删 --device-id
- [ ] 凭据只从 SHANKA_TEST_USERNAME / SHANKA_TEST_PASSWORD；prod 显式确认且禁自动注册
- [ ] auth / isolation / cards-review-stats / pdf / generation / observability 最小场景（无 legacy 场景）
- [ ] 成本闸门：运行前最坏预算推导，运行后 ledger 对账；live 需成本/Key 确认

### P8 总验收

- [ ] 全量工具链（pytest / ruff format / ruff check / mypy）、迁移副本、Android build、平台 quick/full/live
- [ ] 敏感 sentinel 扫描；后台任务 logout 后继续 + 跨用户 404
- [ ] `STATUS.md` 阶段账本完整；`WORKER_REPORT.md` 落盘（改动、命令、退出码、计数、风险、未完成项）
