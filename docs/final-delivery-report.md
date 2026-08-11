# 闪卡 App v2.1 后端 最终交付报告（F0～R1）

日期：2026-08-11｜分支：main @ 6482b3c｜全部工作包 `DONE`（Progress.md 第 4 节）

## 1. 工作包 / 分支 / 验收证据

| 包 | 分支（已合并清理） | 验收命令与结果 | 关键证据 |
| --- | --- | --- | --- |
| F0 可执行基线与护栏 | codex/f0 | pytest 34 / ruff / format / mypy | 守卫框架（schema↔openapi、错误码、localization、manifest）8 用例 |
| F1 数据与 HTTP 共享基础 | codex/f1 | pytest 47 | 12 表 ORM + 迁移、幂等原语、中间件链、metrics |
| V1 牌组与卡片闭环 | codex/v1 | pytest 40 | 幂等 create/list/import、AC-09 |
| V2 FSRS 复习与看板 | codex/v2 | pytest 41 | py-fsrs 4.1.2 固定（R-13）、双幂等、AC-10 |
| V3A PDF 生命周期 | codex/v3a | pytest 40 | 真实样书文本层+书签解析、扫描器、AC-01/02 |
| V3B API Key 与 DeepSeek 边界 | codex/v3b | pytest 40 | AES-GCM 加密、脱敏、AC-11 |
| V4 样卡/任务/规划 | codex/v4 | pytest 43 | manifest 加载、fake 样卡、任务状态机、AC-03 |
| V5A 分批生成与质量观测 | codex/v5a | pytest 24 | Schema 唯一门槛、Rubric 观测、AC-04/07 |
| V5B 任务恢复/取消/并发 | codex/v5b | pytest 8 | 心跳批次事务、孤儿恢复、AC-05、I-1 条件更新 |
| V6 单卡重写闭环 | codex/v6 | pytest 26 | 原地替换、ReviewState 重置、AC-06 |
| R1 回归 + live 验证 | codex/r1 | pytest 5 + live | 见 §2/§3 |

**main 最终组合验收**（每包合并后重跑，最终态）：`python -m pytest` **353 passed**；`ruff check .` All checks passed；`ruff format --check .` 通过；`mypy .` Success（173 files）。全部在 conda env `shanka-backend`（Python 3.12.13）下，依赖锁定 `requirements-dev.lock`（46 钉版本，R-02）。

## 2. Progress 变化

F0 → F1 → V1 → V2 → V3A → V3B → V4 → V5A → V5B → V6 → R1 全部 `DONE`（docs/Progress.md 第 4 节逐包证据）。冲突登记 R-01～R-21（RESOLVED/ACCEPTED/OPEN 见第 6 节）。

## 3. DeepSeek 实际调用 / token / 费用 / 边界（R1 live）

- **模型（冻结）**：`deepseek-v4-flash` + thinking disabled + JSON output；system_fingerprint 单一 `fp_a18b46594c_prod0820_fp8_kvcache_20260402`。
- **正式样本**：60 单元（第 1/2/6 章各 20 块，24/24/12），**59/60 成功**（单元 43 上游 GENERATION_FAILED 抖动）；完成率 98.3%（对照 PRD 8.1 ≥90% ✓）；重复入库率 0%；60/60 幂等重放 ✓。
- **调用账目**：live 驱动运行 3 次（live1/live2 canary 失败 + live3 正式）+ 诊断调用 3 次（canary 根因定位）；正式样本仅运行 1 次（边界内）。
- **token**：prompt 85,599（cache_hit 68,224 / cache_miss 17,375）+ output 195,774。
- **费用**：正式运行 **¥1.6351**；全战役（含 canary/诊断）**¥1.7436**——上限每次 ¥5 / 总计 ¥10 均未触发（R-21 登记）。
- **统计**：失败率 1/60 = 1.67%，Wilson 95% 双侧 [0.29%, 8.86%]（scipy 非依赖，标准库实现；单侧上界约 7.1~7.7%）；仅描述固定抽样框 × 冻结模型，不外推（R-05 口径）。
- **人工复核**：18 张（难度分层固定 seed）无事实错误/前后不匹配，描述性报告见 docs/r1-live-report.md。

## 4. 未验证的外部范围

- Cloudflare Tunnel / TLS 证书 / Android 真机联网（R-06：本期明确不做）。
- OCR/扫描版/图片型 PDF（AC-01 排除项）。
- 多实例 / 生产 DB（PostgreSQL）行级锁语义（R-17 登记：SQLite 单写者 BEGIN IMMEDIATE 是既定架构）。
- 真并发写（SQLite 单写者下 rowcount=0 争抢不可构造——服务器 DB 语义验证）。
- 前端（Android）联调、跨设备同步、账号体系（PRD 明确排除）。

## 5. 剩余冲突 / 风险 / 用户决策事项

| ID | 状态 | 事项 |
| --- | --- | --- |
| R-03 | OPEN | agent v1 资产已版本化；generator 已随 R-20 升 v2，planner/rubric 待精修时再演进 |
| R-05 | ACCEPTED | 成功率/恢复率不可由 60 单元完整证明——已按固定抽样框带条件统计界限报告 |
| R-09 | ACCEPTED | 模型冻结 deepseek-v4-flash + thinking disabled（R1 可比性）；产品配置单一入口可替换 |
| R-14 | OPEN | openapi /samples 响应占位字段（deck_id="" 等）——R1 契约修订定义轻量 SampleCard 未做（本机边界） |
| R-17/18/19 | ACCEPTED | SQLite 单写者锁 500 / version 格式分支 / MANUAL 卡 gen_item 索引外 |
| R-21 | ACCEPTED | API_KEY_ENCRYPTION_KEY 未在 .env 提供（driver 临时密钥，live DB 密文跨进程不可解）——部署侧应提供 |
| — | 风险 | executor chat 期间 cancel → 500 database-locked（R-17）；rewrite prompt 占位符 replace 顺序（卡内容含字面 `{back}` 会被篡改，仅影响 prompt 输入） |
| — | 决策 | 生产部署：数据库选型（SQLite 单写者 vs PostgreSQL）、API Key 加密密钥托管、Cloudflare Tunnel 上线（deployment.md 已定稿，属后续阶段） |

## 6. 交付物清单

- 契约：docs/PRD（需求）、docs/Architecture（structure-contract / database-design / openapi.yaml 三处一致守卫）
- 代码：main/（app / services / infra / domain 单向分层，353 测试）
- 资产：agent_evolution/（prompts v1/v2 + schemas + rubrics + manifest + CHANGELOG）
- 报告：docs/r1-live-report.md（live 全证据）、docs/Progress.md（F0-R1 状态与冲突登记）
- 测试：main/tests/{unit, integration, contract, acceptance, live}（命名 test_<模块>_<行为>）
