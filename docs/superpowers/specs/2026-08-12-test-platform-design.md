# 测试平台设计（2026-08-12）

## 1. 目标与范围

将联调验证实践沉淀为**独立于后端测试代码**的自动化测试平台：分层架构、按后端域组织场景、成本护栏、真机支持。**本次落地第一期**：目录架构 + 核心库 + 两个场景（api_smoke 迁入、live_flow 新建）+ 调度入口 + 真机构建脚本。

不在本次范围：全部域场景的补全（场景地图登记、按需生长）、CI 集成、Web 面板、新测试框架。

## 2. 背景与动因

- 联调验证散落：`main/scripts/smoke_api.py`（无 Key 冒烟）、`/tmp/flow_smoke.py`（临时完整流程脚本，已跑通）、前端构建环境（本机踩通但未脚本化）。
- 用户决策：live 生成每次联调 1-2 次调用可接受；**批量调用必须确认**；测试平台独立于 `main/tests/`；架构先行，不堆砌。
- 仓库约束：依赖与 lint 唯一事实源 `main/pyproject.toml`（本平台因此**零依赖纯 stdlib**）；`.env` 凭据红线（Key 仅从 `.env` 读、不落命令行/日志）；`docs/AGENTS.md` 主目录结构变更需登记 Progress.md。

## 3. 架构（v3 + 自审修正 + 日志设计）

```text
test-platform/                     # 顶层独立目录(git 跟踪)
├── AGENTS.md                      # 平台说明(用 agent-md-maintenance 技能撰写):
│                                  #   分层/用法/场景地图/新增场景指引/日志规范
├── shanka/                        # 核心库(纯 stdlib,零依赖)
│   ├── __init__.py
│   ├── client.py                  # HTTP 抽象:设备头/幂等键/429重试/请求节奏/脱敏/超时
│   ├── cost.py                    # 成本护栏:阈值常量 + 聚合校验函数
│   ├── cleanup.py                 # 数据策略:随机设备/固定设备 + 场景结束清理
│   ├── environments.py            # 环境:local(localhost:8000) / prod(shanka.kbzz1.top)
│   ├── report.py                  # 统一报告:PASS/FAIL 步骤 + 退出码(失败步骤数)
│   └── logging.py                 # JSON Lines 日志:run_id 上下文/字段规范/脱敏
├── logs/                          # 运行时日志输出(git 忽略)
│   └── test-platform.log          #   JSON Lines,固定路径,每行一个事件
├── scenarios/
│   ├── baseline/
│   │   ├── __init__.py
│   │   └── api_smoke.py           # (从 main/scripts 迁入)探针/鉴权/幂等/错误结构/openapi/metrics
│   └── flow/
│       ├── __init__.py
│       └── live_flow.py           # 完整制卡流程:Key→PDF→样卡→任务→复习→看板
├── runner/
│   ├── run.sh                     # 入口:--environment --suite [--scenario] [--confirm-cost] [--confirm-prod]
│   └── suites.py                  # 套件:quick(0 次 LLM 调用,纯无Key冒烟) /
│                                  #       full(非生成场景 + api_key,合计 1 次校验调用) /
│                                  #       live(full + samples/tasks/live_flow,含真实生成,触发成本闸门)
└── device/
    ├── build/
    │   └── build_apk.sh           # WSL2 编译 debug APK(SDK 路径参数化)
    ├── install/
    │   └── install.sh             # adb 安装到已连真机;无设备则提示跳过
    └── AGENTS.md                  # 真机层说明:instrumented 测试执行命令(connectedAndroidTest)
```

### 日志与可观测性（日志是平台可观测性的核心环节）

- **输出**:`test-platform/logs/test-platform.log`(JSON Lines,固定路径,追加式;与后端 `main/data/logs/app.log` 分离但格式同构,一套工具可读)。`logs/` 为运行时产物,git 忽略。文件管理:固定追加,**AGENTS.md 注明归档/清理运维约定**(按 run_id 归档或定期清理;本期不做自动滚动,YAGNI)。
- **输出交互**:console 只输出 report 的人类可读步骤汇总(PASS/FAIL + 耗时);JSON 事件全量只进日志文件,避免双行噪音。
- **事件字段**（每行一个 JSON 事件,对齐后端 app.log 风格）:
  - 必选:`timestamp`(UTC ISO8601)/ `level`(INFO/WARN/ERROR)/ `run_id`(一次 run.sh 全套共用一个 UUID)/ `message`
  - 请求事件另含:`suite` / `scenario` / `step` / `request_id`(后端 X-Request-ID 响应头)/ `device_id` / `method` / `path` / `status` / `duration_ms` / `error_code`
- **记录职责(数据流)**:`client.py` 每次请求后**自动**经 `logging.py` 记录请求事件(含 X-Request-ID);场景只负责业务步骤与步骤标记,不重复记录;`run_id` 由 runner 统一生成并注入所有场景(单场景运行亦然),场景不自行生成。
- **可观测性联动**:
  - `run_id`:一次运行的全套日志共用,按 run 整体拉取/归档;
  - `request_id`:与后端 app.log 同 request_id 交叉核对——一条测试请求从测试平台发起 → 后端中间件 → 业务处理 → 响应,全链路可查(与后端 `X-Request-ID` 约定一致,见 backend-integration.md 2.3 自查方法);
  - 错误事件:仅记 `error_code`,不落明文。
- **脱敏(对齐仓库红线 4)**:`PUT /api-key` 的请求体与响应永不落日志;任何事件不得出现 API Key 明文、设备 ID 仅以 `device_id` 字段出现（不混入 message）。
- **实现归属**:`shanka/logging.py` 提供 JSON 行输出、run_id 上下文管理、字段校验与脱敏。

### 核心原则

1. **域分组即地图**：场景子目录 = 后端 `services/` 域（identity/pdf/cards/generation/review/stats/baseline/flow）。新增场景按域落位；未实装的域只登记在 README 场景地图，**不建空壳文件**（防占位腐化）。
2. **一场景一文件 = 一个用户故事**：场景脚本只写业务步骤，复用 `shanka/` 核心库，不重复 HTTP 细节。
3. **调度与场景分离**：runner 负责环境/套件/场景选择与成本闸门，不掺业务逻辑。
4. **零依赖**：全平台纯 Python stdlib，任何 python3 可跑，不引入 pyproject/venv，天然与 main 环境解耦。
5. **成本闸门**：每个场景模块声明 `LLM_CALLS: int`（预期真实 DeepSeek 调用数：PUT /api-key 校验、POST /samples、POST /tasks 均计数）。runner 聚合套件总数，**超过阈值（默认 3，即 `> 3`）必须 `--confirm-cost`**，否则拒绝执行。批量场景声明数大，天然触发闸门。
6. **环境安全闸门**：目标环境为 prod（shanka.kbzz1.top）时**默认拒绝执行**，必须显式 `--confirm-prod`（防止误操作向生产 DB 写数据）；local 环境默认放行。
7. **数据策略**：默认随机设备 ID + 场景结束自动清理（cleanup.py）；`--device-id` 固定可保留观察。
8. **请求纪律**：继承 smoke_api 的 0.3s 节奏（IP 限流 5 req/s），429 按 Retry-After 重试。

## 4. 场景地图（README 登记，本期不实装）

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

## 5. 迁移清单

- `main/scripts/smoke_api.py` → `test-platform/scenarios/baseline/api_smoke.py`：
  - 依赖从 httpx 改写为 `shanka/client.py`（stdlib）；
  - 保留退出码 = 失败步骤数约定；
  - 保留 openapi 契约校验与 metrics 检查；openapi 来源改为参数化（默认运行时 `/openapi.json`，可选本地文件路径）。
- `main/scripts/AGENTS.md`：更新为「日常冒烟已迁至 test-platform/scenarios/；本目录仅保留部署脚本 run.sh/stop.sh 与验收脚本 live_estimate_smoke.py（R1 历史资产）」。
- `main/scripts/live_estimate_smoke.py`：**不迁移**（R1 验收证据链，Progress.md 引用）。
- 文档引用同步：grep 仓库内对 smoke_api.py 的引用并更新（spec 落地时列出）。

## 6. 第一期交付物

1. `test-platform/` 全目录（AGENTS.md + shanka/ 六模块 + logs/ + runner/ + device/ + 两个场景）
2. `test-platform/AGENTS.md`：用 agent-md-maintenance 技能撰写（分层/用法/场景地图/新增场景指引/日志规范）；`device/AGENTS.md` 同理
3. `live_flow.py`：从 /tmp/flow_smoke.py 正式化——参数化（--base-url/--device-id/--skip-generate）、LLM_CALLS 声明、清理、报告、经 logging.py 输出事件
4. `runner/run.sh` + `suites.py`：quick/full/live 套件、成本闸门（--confirm-cost）与环境安全闸门（--confirm-prod）
5. `device/build/build_apk.sh`、`device/install/install.sh`
6. `main/scripts/AGENTS.md` 更新 + `docs/frontend/local-dev.md` 增「自动化测试平台」章节
7. Progress.md 登记 test-platform/（主目录结构变更）;仓库根 .gitignore 加 `test-platform/logs/`

## 7. 验收

- 零依赖：`python3` 直接运行平台脚本（stdlib 导入验证），不需要 main 的 conda 环境。
- quick 套件在本地后端运行中全绿（退出码 0）；live 套件真实生成需 `--confirm-cost`;prod 环境默认拒绝、需 `--confirm-prod`。
- 日志：运行后 `logs/test-platform.log` 生成 JSON Lines;含 run_id/request_id;**live 套件运行后** grep 校验无 API Key 明文;与后端 app.log 同 request_id 可交叉定位。
- 迁移后 `main/scripts/` 无残留引用；mypy/ruff 不扫 test-platform（不在 main/pyproject 范围）。
- 真机：build_apk.sh 产出 APK；install.sh 在设备连接时安装成功。

## 8. 明确不做（YAGNI）

Web 面板/报告服务器；新测试框架；CI 集成（稳定后再议）；环境编排（Docker 等）；全部域场景一期补齐。
