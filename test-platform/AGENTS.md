# AGENTS.md

自动化联调验证平台：独立顶层目录，与后端 `main/tests/` 分离，零依赖纯 stdlib（任何 `python3` 可跑，不依赖 main 的 conda 环境）。设计权威：`docs/superpowers/specs/2026-08-12-test-platform-design.md`；账号化扩展权威：`docs/account-auth-test-platform-long-run-v1/DESIGN.md` §8。

## 分层结构

```text
test-platform/
├── shanka/       # 核心库（纯 stdlib）：client（HTTP 抽象）/ cost（成本闸门）/ cleanup（数据策略）/
│                 # environments（local/prod）/ account（账号引导 register-or-login）/ report（PASS/FAIL 汇总）/
│                 # logging（JSON Lines 事件）
├── scenarios/    # 场景脚本：按后端 services/ 域分组（auth/isolation/baseline/flow/identity/pdf/cards/generation/review/stats），
│                 # 一场景一文件 = 一个用户故事
├── runner/       # 调度：run.sh 薄壳 → suites.py（环境/套件/场景选择 + 成本/环境闸门）
├── device/       # 真机层：build_apk.sh / install.sh（见 device/AGENTS.md）
└── logs/         # 运行时 JSON Lines 日志（git 忽略：/test-platform/logs/）
```

## 用法命令

- 调度入口：`./test-platform/runner/run.sh --environment local|prod --suite quick|full|live [--scenario NAME] [--confirm-cost] [--confirm-prod]`
- 凭据：测试账号只从环境变量读取——`SHANKA_TEST_USERNAME` / `SHANKA_TEST_EMAIL` / `SHANKA_TEST_PASSWORD`（缺失拒绝执行，不自动注册）；local 环境先 register（账号已存在回落 login），prod 只 login 且必须 `--confirm-prod`（禁止自动注册）。
- 套件：`quick` = auth + api_smoke（0 次 LLM 调用纯冒烟）；`full` = 非生成场景（auth/isolation/api_smoke，0 次 LLM；域场景实装后扩展）；`live` = full + live_flow（真实生成，最坏调用预算由 fixture 推导，超阈值必须 `--confirm-cost`）。**live_flow 复用当前测试用户已解析的 PDF（账号域按 user 隔离数据），需先上传解析或预置测试账号数据**。
- 单场景直跑：`cd test-platform && python3 scenarios/baseline/api_smoke.py [--base-url] [--environment local|prod] [--pace]`；凭据同上读 env，不经 CLI 参数；退出码 = 失败步骤数（0 = 全绿）。
- 平台自测：`cd test-platform && python3 -m unittest discover -s tests`（stdlib 测试，不依赖 main 环境）。

## 场景地图（域场景登记表）

未实装的域只登记、**不建空壳文件**（防占位腐化），实装后同步本表。

| 域 | 场景文件 | 内容 |
| --- | --- | --- |
| auth | auth.py | register/login/me/logout 最小链路、401 语义（AUTH_REQUIRED/INVALID_CREDENTIALS）、prod 禁自动注册 |
| isolation | isolation.py | 两用户资源 404/隔离、observability 按 user；prod 只读断言 |
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

已实装：`auth/auth.py`（NAME=`auth`，LLM_CALLS=0）、`isolation/isolation.py`（NAME=`isolation`，LLM_CALLS=0）、`baseline/api_smoke.py`（NAME=`api_smoke`，LLM_CALLS=0）、`flow/live_flow.py`（NAME=`live_flow`，LLM_CALLS=BUDGET_FIXTURE 推导最坏预算 53——废弃固定 3 假设）。

## 新增场景指引

- 按后端 `services/` 域落位到 `scenarios/<域>/` 新文件（域不存在时建新目录）；场景子目录 = 域分组即地图。
- 每个场景模块必须声明：`NAME`（套件/单跑标识）、`LLM_CALLS: int`（最坏真实 DeepSeek 调用数，供 runner 成本闸门聚合；0 LLM 场景写 0，生成类场景声明 `BUDGET_FIXTURE`（章节/quantity_tendency/generate）由 `shanka/cost.derive_budget` 推导而非手写固定值）、`main(argv) -> int`（返回失败步骤数）。
- 业务步骤复用 `shanka/` 核心库（client 请求、account 会话引导、report 汇总、logging 事件），不重复 HTTP 细节；逻辑层拆出 `run(...)` 供无网络单元测试；完成后在 `runner/suites.py` 注册套件成员。

## 日志规范

- 输出：`logs/test-platform.log`（JSON Lines，追加式，git 忽略；与后端 `main/data/logs/app.log` 格式同构，一套工具可读）。
- 事件字段：必选 `timestamp`（UTC ISO8601）/ `level` / `run_id`（一次 run.sh 全套共用一个 UUID，由 runner 生成注入，场景不自行生成）/ `message`；请求事件另含 `suite` / `scenario` / `step` / `request_id`（后端 X-Request-ID 响应头，可与 app.log 同 request_id 全链路交叉核对）/ `user_id`（账号身份；会话建立前为空，register/login 敏感路径不落事件）/ `method` / `path` / `status` / `duration_ms` / `error_code`。
- 记录职责：`client.py` 每次请求后自动记录，场景只做业务步骤与步骤标记；console 只输出 report 人类可读汇总，JSON 事件全量只进日志文件。
- 脱敏（对齐仓库红线 4）：PUT /api-key 与 register/login 请求/响应永不落日志；API Key、密码、会话 token 明文不出现于任何事件；Authorization 头统一 `Bearer ***`。
- 归档：logs/ 为运行时产物，按 run_id 整体拉取/归档或定期清理（本期不做自动滚动，YAGNI）。

## 成本与环境闸门

- 成本闸门（DESIGN 8.3，废弃「live 固定 3 次调用」假设）：运行前 `shanka/cost.derive_budget` 按受控 fixture（章节数/quantity_tendency/generate/planning_groups）与契约默认上限（镜像 main/app/config.py 与 structure-contract，后端运维调整需同步）推导最坏调用预算（PLANNING/GENERATING/SCORING 调用数与 token 上限，含重试上限；**PLANNING 按 fixture 声明 3 规划组计（前 2 章 42.6k 字符 ÷ planner_max_input_chars 20k 向上取整，受后端 max_planner_groups_per_task=30 上限）；fixture 页文本量或后端拆组阈值调整时，需手工同步此前提声明**）；runner 聚合套件最坏调用数，**超过阈值（默认 3，即 > 3）必须 `--confirm-cost`**，拒绝消息含逐阶段预算明细。
- 运行后对账：live 任务完成后经 `GET /tasks/{id}/batches` 对账实际批数/生成尝试/token/成本（批=单元账本投影，成本以服务端 8.4 常量 `cost_estimate` 为准）写入报告字段（`llm_budget_calls`/`llm_attempts_actual`/`llm_tokens_actual`/`llm_cost_actual`）；**边界：后端无 llm_call_attempts GET 端点，PLANNING/SCORING 尝试数无 HTTP 观测入口，对账只覆盖 GENERATING 阶段，报告须如实声明**。
- 环境安全闸门：目标环境为 prod（shanka.kbzz1.top）时**默认拒绝执行**，必须显式 `--confirm-prod`（防误操作向生产 DB 写数据）；prod 禁止自动注册，只允许已有测试账号登录。
- 请求纪律：继承 0.3s 节奏（IP 限流 5 req/s），429 按 Retry-After 重试；数据策略：业务资源场景结束自动清理（`shanka/cleanup.py`，含异常路径前缀兜底清理），session 一律 logout 撤销；local 临时测试账号以 run_id 命名注册，无法安全删除的 user 行按 run_id 计数写入报告字段（`local_test_users_created`，不新增生产账号删除接口）。
