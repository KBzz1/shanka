# 测试平台设计（2026-08-12）

## 1. 目标与范围

将联调验证实践沉淀为**独立于后端测试代码**的自动化测试平台：分层架构、按后端域组织场景、成本护栏、真机支持。**本次落地第一期**：目录架构 + 核心库 + 两个场景（api_smoke 迁入、live_flow 新建）+ 调度入口 + 真机构建脚本。

不在本次范围：全部域场景的补全（场景地图登记、按需生长）、CI 集成、Web 面板、新测试框架。

## 2. 背景与动因

- 联调验证散落：`main/scripts/smoke_api.py`（无 Key 冒烟）、`/tmp/flow_smoke.py`（临时完整流程脚本，已跑通）、前端构建环境（本机踩通但未脚本化）。
- 用户决策：live 生成每次联调 1-2 次调用可接受；**批量调用必须确认**；测试平台独立于 `main/tests/`；架构先行，不堆砌。
- 仓库约束：依赖与 lint 唯一事实源 `main/pyproject.toml`（本平台因此**零依赖纯 stdlib**）；`.env` 凭据红线（Key 仅从 `.env` 读、不落命令行/日志）；`docs/AGENTS.md` 主目录结构变更需登记 Progress.md。

## 3. 架构（v3 + 自审修正）

```text
test-platform/                     # 顶层独立目录(git 跟踪)
├── README.md                      # 平台说明:分层/用法/场景地图/新增场景指引
├── shanka/                        # 核心库(纯 stdlib,零依赖)
│   ├── __init__.py
│   ├── client.py                  # HTTP 抽象:设备头/幂等键/429重试/请求节奏/脱敏日志/超时
│   ├── cost.py                    # 成本护栏:阈值常量 + 聚合校验函数
│   ├── cleanup.py                 # 数据策略:随机设备/固定设备 + 场景结束清理
│   ├── environments.py            # 环境:local(localhost:8000) / prod(shanka.kbzz1.top)
│   └── report.py                  # 统一报告:PASS/FAIL 步骤 + 退出码(失败步骤数)
├── scenarios/
│   ├── baseline/
│   │   ├── __init__.py
│   │   └── api_smoke.py           # (从 main/scripts 迁入)探针/鉴权/幂等/错误结构/openapi/metrics
│   └── flow/
│       ├── __init__.py
│       └── live_flow.py           # 完整制卡流程:Key→PDF→样卡→任务→复习→看板
├── runner/
│   ├── run.sh                     # 入口:--environment --suite [--scenario] [--confirm-cost]
│   └── suites.py                  # 套件:quick(无Key冒烟) / full(全部非生成) / live(含生成)
└── device/
    ├── build/
    │   └── build_apk.sh           # WSL2 编译 debug APK(SDK 路径参数化)
    ├── install/
    │   └── install.sh             # adb 安装到已连真机;无设备则提示跳过
    └── README.md                  # instrumented 测试执行命令(connectedAndroidTest)
```

### 核心原则

1. **域分组即地图**：场景子目录 = 后端 `services/` 域（identity/pdf/cards/generation/review/stats/baseline/flow）。新增场景按域落位；未实装的域只登记在 README 场景地图，**不建空壳文件**（防占位腐化）。
2. **一场景一文件 = 一个用户故事**：场景脚本只写业务步骤，复用 `shanka/` 核心库，不重复 HTTP 细节。
3. **调度与场景分离**：runner 负责环境/套件/场景选择与成本闸门，不掺业务逻辑。
4. **零依赖**：全平台纯 Python stdlib，任何 python3 可跑，不引入 pyproject/venv，天然与 main 环境解耦。
5. **成本闸门**：每个场景模块声明 `LLM_CALLS: int`（预期真实 DeepSeek 调用数：PUT /api-key 校验、POST /samples、POST /tasks 均计数）。runner 聚合套件总数，超阈值（默认 3）必须 `--confirm-cost`，否则拒绝执行。批量场景声明数大，天然触发闸门。
6. **数据策略**：默认随机设备 ID + 场景结束自动清理（cleanup.py）；`--device-id` 固定可保留观察。
7. **请求纪律**：继承 smoke_api 的 0.3s 节奏（IP 限流 5 req/s），429 按 Retry-After 重试。

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
  - 保留 openapi 契约校验与 metrics 检查。
- `main/scripts/AGENTS.md`：更新为「日常冒烟已迁至 test-platform/scenarios/；本目录仅保留部署脚本 run.sh/stop.sh 与验收脚本 live_estimate_smoke.py（R1 历史资产）」。
- `main/scripts/live_estimate_smoke.py`：**不迁移**（R1 验收证据链，Progress.md 引用）。
- 文档引用同步：grep 仓库内对 smoke_api.py 的引用并更新（spec 落地时列出）。

## 6. 第一期交付物

1. `test-platform/` 全目录（README + shanka/ 五模块 + runner/ + device/ + 两个场景）
2. `live_flow.py`：从 /tmp/flow_smoke.py 正式化——参数化（--base-url/--device-id/--skip-generate）、LLM_CALLS 声明、清理、报告
3. `runner/run.sh` + `suites.py`：quick/full/live 套件与成本闸门
4. `device/build/build_apk.sh`、`device/install/install.sh`
5. `main/scripts/AGENTS.md` 更新 + `docs/frontend/local-dev.md` 增「自动化测试平台」章节
6. Progress.md 登记 test-platform/（主目录结构变更）

## 7. 验收

- 零依赖：`python3 test-platform/runner/... ` 不需要 main 的 conda 环境即可跑（stdlib 导入验证）。
- quick 套件在本地后端运行中全绿（退出码 0）；live 套件默认跳过生成可跑通、真实生成需 `--confirm-cost`。
- 迁移后 `main/scripts/` 无残留引用；mypy/ruff 不扫 test-platform（不在 main/pyproject 范围）。
- 真机：build_apk.sh 产出 APK；install.sh 在设备连接时安装成功。

## 8. 明确不做（YAGNI）

Web 面板/报告服务器；新测试框架；CI 集成（稳定后再议）；环境编排（Docker 等）；全部域场景一期补齐。
