# AGENTS.md

自动化联调验证平台：独立顶层目录，与后端 `main/tests/` 分离，零依赖纯 stdlib（任何 `python3` 可跑，不依赖 main 的 conda 环境）。设计权威：`docs/superpowers/specs/2026-08-12-test-platform-design.md`。

## 分层结构

```text
test-platform/
├── shanka/       # 核心库（纯 stdlib）：client（HTTP 抽象）/ cost（成本闸门）/ cleanup（数据策略）/
│                 # environments（local/prod）/ report（PASS/FAIL 汇总）/ logging（JSON Lines 事件）
├── scenarios/    # 场景脚本：按后端 services/ 域分组（baseline/flow/identity/pdf/cards/generation/review/stats），
│                 # 一场景一文件 = 一个用户故事
├── runner/       # 调度：run.sh 薄壳 → suites.py（环境/套件/场景选择 + 成本/环境闸门）
├── device/       # 真机层：build_apk.sh / install.sh（见 device/AGENTS.md）
└── logs/         # 运行时 JSON Lines 日志（git 忽略：/test-platform/logs/）
```

## 用法命令

- 调度入口：`./test-platform/runner/run.sh --environment local|prod --suite quick|full|live [--scenario NAME] [--confirm-cost] [--confirm-prod] [--device-id UUID]`
- 套件：`quick` = 0 次 LLM 调用纯无 Key 冒烟；`full` = 非生成场景 + api_key（合计 1 次校验调用，域场景实装后扩展）；`live` = 完整制卡流程（真实生成，触发成本闸门）。**live 套件需固定 `--device-id` 运行（随机设备无预置 PDF——后端按设备隔离数据，live_flow 复用已解析 PDF 须与预置数据同设备）**。
- 单场景直跑：`cd test-platform && python3 scenarios/baseline/api_smoke.py [--base-url] [--device-id] [--pace]`；退出码 = 失败步骤数（0 = 全绿）。
- 平台自测：`cd test-platform && python3 -m pytest tests/`（stdlib 测试，不依赖 main 环境）。

## 场景地图（6 域 14 场景登记表）

未实装的域只登记、**不建空壳文件**（防占位腐化），实装后同步本表。

| 域 | 场景文件 | 内容 |
| --- | --- | --- |
| identity | api_key.py | PUT/GET status 校验语义（AVAILABLE/INVALID 不覆盖） |
| identity | idempotency.py | 幂等重放与冲突（C-04） |
| identity | rate_limit.py | 429 与 Retry-After |
| pdf | upload_parse.py | 上传→PARSED→章节（含扫描件边界） |
| pdf | chapters.py | 章节 PATCH/DELETE |
| cards | deck_crud.py | 牌组增删改/进度 |
| cards | card_crud.py | 卡片增删改/列表 |
| cards | import_cards.py | 批量导入原子性 |
| cards | rewrite.py | 单卡重写保留原卡语义 |
| generation | samples.py | 样卡预览 |
| generation | tasks.py | 任务创建/轮询/取消/恢复 |
| generation | estimate.py | 价格预估 |
| review | ratings.py | 四档评级/排程状态 |
| stats | dashboard.py | 看板/时区/空态 |

已实装：`baseline/api_smoke.py`（NAME=`api_smoke`，LLM_CALLS=0）、`flow/live_flow.py`（NAME=`live_flow`，LLM_CALLS=3）。

## 新增场景指引

- 按后端 `services/` 域落位到 `scenarios/<域>/` 新文件（域不存在时建新目录）；场景子目录 = 域分组即地图。
- 每个场景模块必须声明：`NAME`（套件/单跑标识）、`LLM_CALLS: int`（预期真实 DeepSeek 调用数；PUT /api-key 校验、POST /samples、POST /tasks 均计数，批量场景声明大数天然触发成本闸门）、`main(argv) -> int`（返回失败步骤数）。
- 业务步骤复用 `shanka/` 核心库（client 请求、report 汇总、logging 事件），不重复 HTTP 细节；完成后在 `runner/suites.py` 注册套件成员。

## 日志规范

- 输出：`logs/test-platform.log`（JSON Lines，追加式，git 忽略；与后端 `main/data/logs/app.log` 格式同构，一套工具可读）。
- 事件字段：必选 `timestamp`（UTC ISO8601）/ `level` / `run_id`（一次 run.sh 全套共用一个 UUID，由 runner 生成注入，场景不自行生成）/ `message`；请求事件另含 `suite` / `scenario` / `step` / `request_id`（后端 X-Request-ID 响应头，可与 app.log 同 request_id 全链路交叉核对）/ `device_id` / `method` / `path` / `status` / `duration_ms` / `error_code`。
- 记录职责：`client.py` 每次请求后自动记录，场景只做业务步骤与步骤标记；console 只输出 report 人类可读汇总，JSON 事件全量只进日志文件。
- 脱敏（对齐仓库红线 4）：PUT /api-key 请求体与响应永不落日志；API Key 明文不出现于任何事件；设备 ID 仅以 `device_id` 字段出现，不混入 message。
- 归档：logs/ 为运行时产物，按 run_id 整体拉取/归档或定期清理（本期不做自动滚动，YAGNI）。

## 成本与环境闸门

- 成本闸门：runner 聚合套件 LLM_CALLS 总数，**超过阈值（默认 3，即 > 3）必须 `--confirm-cost`**，否则拒绝执行；批量场景声明数大，天然触发闸门。
- 环境安全闸门：目标环境为 prod（shanka.kbzz1.top）时**默认拒绝执行**，必须显式 `--confirm-prod`（防误操作向生产 DB 写数据）；local（localhost:8000）默认放行。
- 请求纪律：继承 0.3s 节奏（IP 限流 5 req/s），429 按 Retry-After 重试；数据策略默认随机设备 ID + 场景结束自动清理（`shanka/cleanup.py`），`--device-id` 固定可保留观察。
