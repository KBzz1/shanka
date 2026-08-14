# WORKER_REPORT — 合并长程任务包（LLM 链路升级 + 账号登录与测试平台）

- 生成：2026-08-14 Asia/Shanghai
- Goal ID：`shanka-llm-account-long-run-v1`（P1 LLM 升级 → P2–P8 账号体系）
- 范围：P1 起所有阶段实际改动、命令证据、计数、残余风险与未完成项。

## 1. 阶段完成总览

| 阶段 | 状态 | 关键证据（主 Worker 实测） |
| --- | --- | --- |
| P1 LLM 链路升级（17 任务） | DONE | 500/0 四工具全绿；canary 3/3 全 PASS（≈¥0.06 ≤ ¥3）；R-03 RESOLVED；LLM_BASELINE_COMMIT=a874944 |
| P2 契约 V2.2 | DONE | PRD V2.2 + 四契约文档原子同步；500/0 四工具全绿 |
| P3 数据地基 | DONE | Alembic 链 3 revisions + fail-closed + 旧行守恒；508/0 四工具全绿 |
| P4 后端切换 | DONE | auth 四端点 + Bearer + 全链路 user_id + X-Device-ID 退出；557/0 四工具全绿 |
| P5 后台 user_id 接续 | DONE | 6 判别测试（logout/过期继续、session 零依赖、跨用户 404、metrics 无身份）；563/0 |
| P6 Android | DONE | frontend-app 4 commits；40/40 JVM + assembleDebug 全绿；X-Device-ID 零残留 |
| P7 test-platform v2 | DONE | 77/0 平台自测 + unittest 复跑稳定；成本闸门预算推导/批次对账 |
| P8 总验收 | DONE | 见 §2 验收命令（全部真实运行） |

## 2. P8 总验收命令与退出码（2026-08-14 主 Worker 实测）

```
# main/ 四工具
cd main && conda run -n shanka-backend python -m pytest
  → 563 passed, 0 failed（exit 0，155.73s）
conda run -n shanka-backend python -m ruff check .        → All checks passed（exit 0）
conda run -n shanka-backend python -m ruff format --check . → 272 files already formatted（exit 0）
conda run -n shanka-backend python -m mypy .              → Success: 219 source files（exit 0）

# 迁移副本往返（临时库，非生产）
alembic upgrade head    → 6 revisions 全链（initial→0002→0003→ddc6→a7cc→e85c）exit 0
alembic check           → No new upgrade operations detected（exit 0）
alembic downgrade 2284b238e3d4 → 5 步反向全成功（exit 0）
alembic upgrade head    → 5 步再全链（exit 0）；alembic check exit 0

# test-platform（P7 证据，本机 2026-08-14 复验）
cd test-platform && python -m pytest tests/  → 77 passed（exit 0）
python -m unittest discover -s tests        → Ran 77 tests OK（多轮复跑稳定）

# Android（P6 证据）
cd frontend-app/Front && ./gradlew test    → 40/40 全绿
./gradlew assembleDebug + assembleDebugAndroidTest → BUILD SUCCESSFUL

# 敏感扫描（P8 实测）
grep 明文 sk-（app/services/infra）：零命中
grep access_token/password 于日志输出：零命中
test_sensitive_redaction 唯一随机 sentinel 机制在位（P4-T6a）
```

## 3. 关键迁移计数

- Alembic revisions：**6**（2284b238e3d4 initial → ead86a96d103 → 2a391e994f93 LLM → ddc6f34e30b8 账号地基 → a7cc699f3fd8 主键重建+fail-closed → e85c78b2a345 api_keys UNIQUE(device_id)）。
- 新表：users、auth_sessions、text_chunks、llm_call_attempts。
- owner 表 user_id 列：8 个直接归属表（pdf_files/tasks/decks/cards/review_events/llm_call_attempts/api_keys/idempotency_keys）。
- 提交计数：main 分支 P1 24 commits（8cd0cb5..a874944）+ P2–P5 19 commits（3464e9c..8eeeca7）+ P6 收尾文档 1（88cdec3）+ P7 5（fae1485/1819e08/594d4c0/4a7935a/1ff05c1）+ 本报告收尾；嵌套 frontend-app 仓库 P1 1 + P6 4 = 5 commits。

## 4. 未运行项（如实声明，不虚报）

1. **平台 quick/full/live 对真实后端联调未运行**——P7-T3 期间本机后端未启动（localhost:8000 拒绝连接，curl 实测 exit 7）；仅以受控最小路径验证 CLI 形状、凭据门与成本闸门。
2. **真实 LLM 调用未运行**（P7 live）——需成本/Key 单独确认，本包未获确认。（P1 canary 的真实 DeepSeek 调用已获用户授权并完成，属 LLM 升级验收。）
3. **Android instrumented 设备测试未运行**——本机无模拟器/真机；仅编译 + 打包验证（BackendClientInstrumentedTest/FlashcardsAppTest 语义已更新待设备验证）。

## 5. 残余风险与未完成项（登记）

1. 对账边界：后端无 llm_call_attempts GET 端点，平台成本对账仅 GENERATING 阶段投影（PLANNING/SCORING 尝试数无 HTTP 观测入口）；P7 已三处声明。
2. PLANNING 预算「1 规划组」前提（前 2 章页文本 ≤ 20k 字符）——欠报方向已文档化（P7 final review fix）；fixture 或后端拆组阈值调整需手工同步（AGENTS.md 已立纪律）。
3. P7 final review Minor（登记不阻塞）：api_smoke.py:116 r.json.get 无 isinstance 守卫；DESIGN 8.2 幂等键/client_event_id 跨用户复用无平台场景覆盖；live_flow obs 账号 bootstrap 失败路径无 WARN；live_flow.py:37 .env 绝对路径（既有代码）。
4. 遗留（各阶段已登记）：driver report["device_id"] 字段（reviewer 判定不违反 §4.5）；write 桶 60s 窗口 clock 注入；FlashcardsAppTest.storedSessionEntersTheMainScreen 非 hermetic；logout 先网络后本地；tests/live driver dry-run mock 形状；REWRITE 孤儿 STARTED 无恢复路径（P1 遗留）；PRD 5.4.2 行 231/238 残留（V2.2 收敛项）。
5. 旧 device_id 数据保留在库（D-06：不迁不删、无访问路径）；清理属后续独立发布，需用户另行批准。
6. 生产迁移/部署未授权执行——一切迁移验证在临时库/副本。

## 6. 完成定义核对（DESIGN §5 / 最终门禁）

- P1：17 任务 + canary + R-03 RESOLVED + LLM_BASELINE_COMMIT ✓
- V2.2 正式契约与实现一致 ✓（P2 契约 + P4 实现 + P8 守卫全等 563/0）
- X-Device-ID 已退出普通运行时认证/授权面 ✓（grep 零注入：main/app、main/services、test-platform、frontend-app 全仓）
- 用户隔离、会话、幂等与敏感数据测试通过 ✓（P4/P5 判别测试 + 敏感脱敏判别）
- Planner/Generator/Scoring/Rewrite 与 ledger 已按 user_id 接续（语义冻结不回改）✓（P5 + agent_evolution 零提交核对）
- Android 与 test-platform v2 已完成并验证 ✓（40/40 + assembleDebug；77/0 + unittest 稳定）
- 全量质量工具真实通过，未运行项明确报告 ✓（§2/§4）
- WORKER_REPORT.md 已记录 ✓（本文件）
