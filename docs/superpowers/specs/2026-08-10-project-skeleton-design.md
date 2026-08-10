# 设计规格：项目骨架 / agent 资产版本化 / Cloudflare 接入 / 可观测性契约补齐

- 日期：2026-08-10
- 状态：已确认（用户逐节批准）
- 上游：PRD v2.1、架构契约五件套（docs/Architecture/）

## 1. 背景与目标

仓库目前只有文档（PRD + 架构契约），`main/` 为空。本次设计一次性解决四件事：

1. 在 `main/` 搭建纯空文件骨架，目录结构对齐 `project-structure.md`，为后续 Phase 0~3 的代码落地做编排。
2. 新增与 `main/` 并行的 `agent_evolution/` 目录，承载 agent（制卡 AI 链路）的版本化资产（prompt / schema / rubric），解决契约中 `prompt_version` / `schema_version` / `rubric_version` 字段"有名字、无载体"的问题。
3. 设计 Cloudflare Tunnel 接入（域名已在 Cloudflare 管理），并给出延迟不可接受时的灰云迁移阶梯。
4. 评估 agent 可观测性现状（结论：字段层完备、机制层缺失），将补齐方案写入契约文档；同步补齐评估骨架（Rubric 执行框架）。

## 2. 已确认决策

| 主题 | 决策 |
| --- | --- |
| main 骨架深度 | 纯空文件骨架：目录全建、模块空文件（`__init__.py` 带一行 docstring 注明对应契约章节）、无任何逻辑 |
| agent 对象 | 制卡 AI 链路（PDF → 规划 → 分批生成 → Schema 校验 → Rubric 评分） |
| 版本化抽取边界 | 只抽版本化资产（prompt / schema / rubric 文本 + 版本日志）；main 的 `infra/llm/` 实现时从资产加载 |
| 版本化形态 | 目录快照 + manifest：每版本一个不可变目录，`manifest.json` 记录当前生效版本 |
| Cloudflare | Tunnel 方案（无公网 IP、无开放端口、无备案）；域名已在 Cloudflare 管理 |
| 可观测性补齐 | 入契约：本次只写设计/契约文档，不写实现代码 |
| 并行目录命名 | `agent_evolution/` |
| 后台任务架构 | 进程内调度器 + DB 即状态；不引外部任务队列（Celery/RQ） |
| 数据演进路径 | 账号：`users` 表 + `devices.user_id` 可空外键；新卡类型：专用列模式（D-01） |
| 工具链 | 骨架含 `pyproject.toml`（ruff/mypy）+ pre-commit；CI 待远端仓库后补 |
| 守卫范围 | contract 测试覆盖四项一致性（schemas↔openapi / ORM↔database-design / 错误码↔契约 7 章 / localization_key↔文案） |

## 3. 仓库布局

```text
shanka_backend/
├── docs/
│   ├── PRD/V2.1/
│   └── Architecture/            # 契约文档（本次更新，见第 8 节）
├── main/                        # 后端实现：纯空文件骨架
│   ├── app/  domain/  services/  infra/  tests/
└── agent_evolution/             # ★ 新增：agent 版本化资产目录
    ├── manifest.json
    ├── CHANGELOG.md
    ├── prompts/
    ├── schemas/
    └── rubrics/
```

依赖方向不变：`docs/PRD → docs/Architecture → main/`。`agent_evolution/` 是 main 的实现资产源（运行时加载），不反向依赖 main。

## 4. main 纯空文件骨架

按 `project-structure.md` 第 3 节逐目录建文件。每个模块建 `__init__.py`（docstring 注明对应契约章节），不写逻辑；`main.py` / `tests/` 各层建空壳。

```text
main/
├── app/
│   ├── api/            # pdfs / api_key / samples / tasks / decks / cards / review / stats
│   ├── middleware/     # device_id / idempotency / error_handler / rate_limit
│   ├── schemas/        # 与 openapi.yaml 对齐
│   └── main.py         # 应用装配（空壳）
├── domain/             # pdf_file / chapter / task / knowledge_point / batch /
│                       # deck / card / review_state / review_event / api_key / enums
├── services/
│   ├── pdf/  generation/  scheduling/  decks/  stats/
├── infra/
│   ├── db/  storage/  llm/
└── tests/
    ├── unit/          # domain、schemas 纯逻辑
    ├── integration/   # services 编排、DB 事务边界
    ├── contract/      # 守卫四项（9.4）：schemas↔openapi、ORM↔database-design、错误码↔契约 7 章、localization_key↔文案
    └── acceptance/    # AC-01~AC-11 验收映射
```

## 5. agent_evolution 资产目录

### 5.1 结构

```text
agent_evolution/
├── manifest.json          # 当前生效版本索引 —— 运行时加载的唯一入口
├── CHANGELOG.md           # 每次演进：原因 + 变更摘要 + 日期
├── prompts/
│   ├── v1/planner.md      # 知识点规划 prompt
│   ├── v1/generator.md    # 分批生成 prompt
│   └── v2/generator.md    # 演进版本示例
├── schemas/
│   └── v1/card.schema.json
└── rubrics/
    └── v1/
        ├── rubric.md          # 四维评分档位表（契约 3.9 落地载体）
        └── scoring-prompt.md  # LLM-as-judge 评分 prompt
```

> 注：`v2/` 目录仅为展示"版本快照"结构形态的示例。**本期只建各资产 `v1` 首版**，`manifest.json` 中所有 version 均指向 `v1`。

### 5.2 manifest.json 形态

```json
{
  "prompts":   { "planner":   {"version": "v1", "path": "prompts/v1/planner.md" },
                 "generator": {"version": "v1", "path": "prompts/v1/generator.md" } },
  "schemas":   { "card":      {"version": "v1", "path": "schemas/v1/card.schema.json" } },
  "rubrics":   { "main":      {"version": "v1", "path": "rubrics/v1/rubric.md" } }
}
```

> 上例为本期创建时形态（全部 version 指向 `v1`）。演进后形态示例见 5.1 目录树（`generator` 升级时新增 `v2/` 目录，manifest 中 version 改为 `v2`、path 改为 `prompts/v2/generator.md`）。

### 5.3 版本语义

- 演进 = 新增版本目录 + 更新 manifest + CHANGELOG 记录原因；回滚 = 改 manifest；审计 = 目录即历史。
- 版本目录内容不可变：修改已有版本 = 新建版本目录。
- 契约字段值闭环：`Batch.prompt_version / schema_version / rubric_version` = manifest 中对应 version 值。
- main 的 `infra/llm/` 实现时按 manifest 解析路径加载资产，代码与资产解耦。

## 6. Cloudflare Tunnel 接入

### 6.1 架构

```text
Android 前端 ──HTTPS──▶ api.<domain>（Cloudflare 边缘，DNS + 自动 TLS 证书）
                             │  Tunnel（出站长连接，无公网 IP / 开放端口）
                             ▼
                    cloudflared（WSL2 本地常驻）
                             │
                             ▼  http://localhost:8000
                    FastAPI（main）
```

### 6.2 子域名规划

| 子域名 | 用途 | Tunnel 路由 |
| --- | --- | --- |
| `api.<domain>` | 生产 API（App 连接） | `localhost:<port>`（默认 8000，可配置） |
| `dev.api.<domain>` | 开发联调 | 同上或独立端口 |

### 6.3 接入要点（设计定稿；实际实施推迟到最后阶段）

1. **本地端口可配置**：FastAPI 监听端口为配置项（默认 `8000`，环境变量覆盖）。实际实施时先检测端口占用，被占用则换端口并同步更新 Tunnel 路由；`api.<domain>` 指向的本地端口以 Tunnel 配置为准。
2. Cloudflare Zero Trust → Networks → Tunnels → 创建命名隧道（如 `shanka-api`），记录 Tunnel Token。
3. WSL2 安装 cloudflared，常驻运行（systemd / nohup）。
4. 公共主机名配置：`api.<domain>` → `localhost:<port>`（端口跟随第 1 条）。
5. TLS：边缘到用户自动 HTTPS；边缘到本机回源走 Tunnel 内部加密，回源不暴露端口。
6. 可选加固：WAF 自定义规则（限流）；`/metrics` 只走 dev 子域名或加 Access。

> **实施时机**：Tunnel 实际接入（cloudflared 安装、隧道创建、DNS/路由配置）**推迟到最后阶段执行**（见第 11 节实施顺序与 Progress.md）；本期仅完成设计文档（deployment.md）。

### 6.4 与现有契约衔接

- 契约 1.7 全部接口 HTTPS：边缘层即 TLS 终止，天然满足。
- 契约 1.6 应用层限流是兜底，CF 边缘限流是外层防线，两层互补。

### 6.5 大陆访问延迟：阶梯决策（不现在选死）

| 阶段 | 方案 | 成本 |
| --- | --- | --- |
| MVP 开发联调 | Tunnel + CF 边缘，真机实测（移动网络通常走香港节点，100~250ms） | 零 |
| 实测不可接受 | 灰云 + 香港 VPS：CF 只做 DNS（灰云），直连 VPS，Nginx + Let's Encrypt（CF DNS-01 challenge 可签发）；需自管反代/证书/防火墙 | 每月约 $5 |
| 真实大陆用户 | 国内云 + ICP 备案（服务器 + 域名双备案） | 最高 |

- 灰云模式与 Tunnel 不兼容（Tunnel 必须走 CF 边缘）；升级 = 部署 VPS → 改 DNS（橙云变灰云）→ 迁移证书。代码层不受影响。
- 决策依据：闪卡 App 是低频短请求（轮询/评级/看板），且前端有 Room 本地缓存，100~250ms 对体验无感知差异；MVP 用户量小，不应为规模问题提前优化。

## 7. 可观测性契约补齐

### 7.1 现状评估结论（写进 spec 供追溯）

- 字段层完备：单卡 Rubric 分、整批质量分布、Cache tokens、版本字段均有落点（Card / Batch）。
- 机制层缺失：无结构化日志规范、无健康检查、无指标出口、无追踪；唯一观测接口（6.9）仅单任务视图，PRD 8.2 跨任务聚合口径无法核验；Rubric 评分过程无痕；版本字段无资产载体（由 agent_evolution/ 解决）。

### 7.2 补齐项（全部入契约；**观测范围限定 DeepSeek API**，其他模型厂商不做）

| 编号 | 补齐项 | 内容 |
| --- | --- | --- |
| O-1 | 结构化日志规范 | JSON 单行；统一字段 timestamp/level/request_id/device_id/task_id/batch_id/error_code/message；级别规范（INFO/WARN/ERROR）；中间件生成 request_id 贯穿，后台批处理以 task_id+batch_id 关联；红线 1.5/7.1 保留 |
| O-2 | 健康检查 | `GET /healthz`（存活）、`GET /readyz`（DB 连接 + 存储可写，失败 503） |
| O-3 | 指标 | `GET /metrics`（Prometheus 文本）；业务：任务终态计数/耗时/批次重试/限流命中；LLM：请求数（model/状态码）/耗时/token 分桶（**model 维度限定 DeepSeek 模型族**）；框架：HTTP 请求数/耗时；`/metrics` 生产子域名默认不暴露 |
| O-4 | 聚合观测接口 | `GET /v1/observability/quality-summary?group_by=model|pdf|difficulty&days=30`：Rubric 各维平均分、覆盖/重复率均值、任务完成率、**成本汇总**（见 O-6）；按当前 device_id 聚合（与业务同隔离），跨设备聚合留给未来后台 |
| O-5 | 评估骨架补全 | 评分执行者明确为 LLM-as-judge；评分 prompt 资产落 `agent_evolution/rubrics/v1/scoring-prompt.md`；评分请求记录 prompt 版本 + 输入摘要 + 输出分（不落完整 prompt）；版本字段值 = manifest version |
| O-6 | 成本观测（经济指标） | 原始 token 数据（cache_hit/cache_miss/output）已入 Batch 表，**不变**；**估算成本**在聚合时按"价格配置常量"换算——常量取 DeepSeek 官方定价、标注生效日期，不固化进 DB（价格调整只改配置，不动历史数据）；O-3 指标与 O-4 聚合接口增加成本汇总（hit/miss/output 分开计价，给出估算金额） |

## 8. 契约文档更新清单

| 文档 | 更新内容 |
| --- | --- |
| `docs/Architecture/project-structure.md` | 仓库总览加 `agent_evolution/`；新增"测试策略"章节（四层职责、命名规范、契约测试验证守卫四项、幂等/并发/级联行为必须进 integration 层）；新增"开发工具链"小节（9.3）；`tests/` 目录说明 |
| `docs/Architecture/structure-contract.md` | 新增"运行可观测性"章节（O-1/O-2/O-3）；新增 6.10 聚合观测接口（O-4）；评估骨架说明（O-5）；后台任务执行架构定式（9.1） |
| `docs/Architecture/database-design.md` | 新增"演进路径"章节（9.2 账号体系 / 新卡类型原则）；迁移工具选型（Alembic） |
| `docs/Architecture/openapi.yaml` | 新增 `/healthz`、`/readyz`、`/v1/observability/quality-summary` |
| `docs/Architecture/deployment.md` | **新增文档**：Cloudflare Tunnel 接入（第 6 节）+ 灰云迁移阶梯 |
| `docs/Architecture/README.md` | 文档清单更新（新增 deployment.md、引用 agent_evolution/） |
| `docs/Progress.md` | 任务表更新：P0-0 骨架（含 pyproject/pre-commit）、P0-4 可观测性、P3-4 Tunnel 实操最后实施 |

## 9. 演进性设计补全（防屎山）

### 9.1 后台任务执行架构

- **MVP 定式**：API 进程内调度器——进程内后台循环扫描 PENDING 任务/批次执行（PDF 解析、规划、分批生成）；任务/批次状态 + 游标存 DB，**DB 即状态**，不引入外部任务队列（Celery/RQ/Redis）。
- **演进路径**：现有契约已隐含多实例兼容——孤儿 RUNNING 心跳恢复（30 分钟）+ DB 条件更新抢占天然适配多 worker；未来灰云多实例只需加 DB 轮询调度，业务代码不动。
- 明确禁止"为性能提前引队列"；P2-2 / P2-4 实现时必须遵循此定式。

### 9.2 数据模型演进路径（写入 database-design.md 新增"演进路径"章节）

- **账号体系**：未来引入 `users` 表 + `devices.user_id` 可空外键（先 NULL 后回填），**不重构 devices 主键**；匿名设备 ID 体系维持为兼容层。
- **新卡类型**：沿用 D-01 模式（专用列 + `front`/`back` 通用渲染）。类型数可控（≤5）时用专用列；字段高度异构或继续膨胀时再评估 JSON 扩展列；所有结构变更走迁移工具（Alembic）。

### 9.3 依赖与工具链

- 骨架阶段创建 `pyproject.toml`：依赖声明 + 工具配置（ruff format/lint、mypy type-check）；依赖锁定文件。
- `pre-commit` 本地钩子：format → lint → type-check。
- CI 等远端仓库就绪后再补（GitHub Actions 等）；本期不建。

### 9.4 自动化守卫范围（contract 测试覆盖四项）

1. `app/schemas` ↔ `openapi.yaml`
2. `infra/db` ORM ↔ `database-design.md`
3. 错误码清单 ↔ 结构契约第 7 章
4. `localization_key` ↔ 文案资产清单

写入 project-structure.md 测试策略章节，作为四条一致性红线的验证手段。

### 9.5 配置分层

- pydantic-settings 单层配置类；默认值进代码；密钥/令牌（API Key 加密密钥等）走环境变量；敏感项清单文档化；禁止散落硬编码。
- 写入 P0-1 实现要求。

## 10. 明确不做（YAGNI）

- 不写任何可运行代码（本期为纯骨架 + 契约）。
- 不引入外部任务队列（Celery/RQ/Redis）——MVP 用 DB 即状态 + 进程内调度（9.1）。
- 不引入 OpenTelemetry / 追踪系统（MVP 用 request_id + 结构化日志 + 已观测字段，满足即可；P3-3 后按实测再评估）。
- 不做跨设备聚合观测接口（数据出口合法性优先）。
- 不做多模型厂商 API 观测适配（观测范围仅 DeepSeek）。
- 不实现 CF 边缘 WAF 规则细节（入 deployment.md 为可选项）。
- 不建测试用例（本期只建目录 + 策略契约）。
- 不建 CI（远端仓库就绪后补）。

## 11. 后续实施顺序（writing-plans 输入）

1. 建 `main/` 空文件骨架（含 tests/ 四层目录、`pyproject.toml` + pre-commit 配置）。
2. 建 `agent_evolution/`（manifest + CHANGELOG + 各资产 v1 首版，资产内容由契约/PRD 推导）。
3. 更新契约文档（第 8 节清单；含 O-6 成本观测、9.1 任务架构、9.2 演进路径）。
4. 写 deployment.md（Tunnel 设计定稿；实际接入为**最后阶段**任务，见 Progress.md P3-4）。
5. 提交。
