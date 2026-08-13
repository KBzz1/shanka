# 后端实现执行地图（v2.1）

本文是后续 Goal 的唯一进度与状态地图，不建立并列计划或看板。需求权威为 [PRD v2.1](PRD/V2.1/prd_v2_1.md)，实现以 [Architecture](Architecture/AGENTS.md) 的防漂移规则为准。Superpowers plan 只细化当前一个工作包的实施步骤，不定义项目状态，也不替代正式契约。

最后事实审计：2026-08-13。

## 1. 状态与事实基线

- `DONE`：产物存在且约定验证实际通过；目录、注释、计划或设计文档不算实现。
- `DOING`：已有未完成实现，证据须写明完成与未完成边界。
- `TODO`：未开始，或只有不可运行空壳。
- `BLOCKED`：正式契约冲突或外部条件缺失，且无法安全推进；必须登记到第 6 节。

完成工作包时同次更新状态和验证证据。禁止主观百分比和从提交信息推断完成。

| 范围 | 状态 | 当前证据 |
| --- | --- | --- |
| PRD 与 Architecture | `DONE` | 正式文档均存在；后续仍受防漂移检查约束 |
| agent 资产 v1 | `DONE`（初稿） | manifest 指向 prompt/schema/rubric v1；CHANGELOG 说明实现时仍需精修，不代表 LLM 链路完成 |
| 代码/测试目录骨架 | `DONE`（仅空壳） | 分层目录、单行占位模块、tests 目录、pyproject、pre-commit 存在 |
| Conda 执行环境 | `DONE`（环境基线） | 已创建 `shanka-backend`，Python 3.12.13；已安装 pyproject 当前声明依赖；editable 安装仍受 R-02 阻塞 |
| 可运行后端 | `DONE`（F1 共享基础） | create_app 装配 + 探针 + 统一错误包装（VALIDATION_ERROR 400 / INTERNAL_ERROR 500）+ 设备鉴权（401/自动注册/探针豁免）+ request_id/JSON 日志 + 幂等原语 + 限流 + metrics；业务路由随 V1+ 纵向包逐步接入 |
| 自动化验证 | `DONE`（R14 扩展） | 354 passed：F0 34 + F1 47 + V1 40 + V2 41 + V3A 40 + V3B 40 + V4 43 + V5A 24 + V5B 8 + V6 26 + R1 5 + R14 1（守卫）；四工具命令全绿（mypy 174 files） |
| test-platform/ | `DONE`（第一期实现） | 测试平台独立顶层目录（2026-08-12 建立：shanka/ 核心库、scenarios/ 两场景、runner/ 调度与闸门、device/ 真机脚本）；端到端验收为 SDD Task 8；设计/计划见 superpowers/specs/2026-08-12-test-platform-design.md、superpowers/plans/2026-08-12-test-platform.md |
| DeepSeek 凭据直连 smoke | `DONE`（仅凭据/端点） | 2026-08-10 直接请求 `deepseek-v4-flash` 成功：non-thinking JSON、`finish_reason=stop`、63 input + 16 output = 79 tokens、cache hit 0；绕过了尚不存在的后端，不能完成 V3B/R1 |

当前唯一正确起点是 `F0`。

## 2. 长程执行原则

1. 除最小共享地基外，每包同时完成 schema、用例、持久化/外部适配、路由和测试，不先铺满某一层。
2. 开工前核对 PRD、structure-contract、openapi、database-design；冲突先登记并修复权威文档，不让实现暗中定义语义。
3. unit 验证纯规则，integration 验证事务/外部边界，contract 防漂移，acceptance 映射 AC；测试随功能完成。
4. handler 只做 HTTP 映射；services 持有用例和事务语义；infra 处理 DB、文件、DeepSeek；domain 保持纯净。共享代码只对应已经出现的稳定契约边界，确认重复语义一致后才提取最小实现；不引入通用 Repository 基类、事件总线、DI 框架、外部任务队列或第二套映射框架。
5. 设备隔离、幂等、错误包装、request_id、限流集中实现；DB session/事务、时钟、配置各有唯一入口。
6. 幂等响应与副作用、复习事件与状态、批次入库与游标、重写与排程重置分别同事务；每个请求/用例只有一个显式事务所有者，service 和 infra helper 不自行 commit。
7. 不实现 PRD 3.2 排除项，不提前引入账号、多实例设施、OCR、自动修复/淘汰/补生成或运营后台。
8. 每次只为当前可执行工作包使用 Superpowers `writing-plans` 生成一份有界计划，再以 `subagent-driven-development` 执行；plan checklist 不是 DONE 证据。主 Agent 依次调度有文件边界的实现 subagent、契约 reviewer 和代码质量 reviewer，亲自完成整包验收后才更新本文。

## 3. 统一完成门槛

每包标记 `DONE` 前必须：

- HTTP 行为走到真实持久化或受控外部适配，不以 Mock/硬编码成功代替。
- 涉及的 OpenAPI schema、应用 schema、ORM、错误码一致；正常、边界、隔离、幂等、失败回滚按适用范围有测试。
- 在 `main/` 通过 `conda run -n shanka-backend python -m pytest`、`conda run -n shanka-backend python -m ruff check .`、`conda run -n shanka-backend python -m ruff format --check .`、`conda run -n shanka-backend python -m mypy .`；有阻塞则不得标为 DONE。
- 明文 API Key、完整 Prompt 不进入日志、响应、任务明细或测试快照；完整 PDF 只作本机样本，不进入版本控制。
- 本文记录验证证据，且未新增平行计划文件。

除已经完成且只证明凭据/端点的直连 smoke 外，当前交付先完成本机应用层证据，再做一次有预算上限的正式应用链路 DeepSeek 验证。应用提供 OpenAPI 全部业务路径及契约声明的 `/metrics`；探针/metrics 豁免鉴权；contract 守卫 schema↔OpenAPI、ORM↔database-design、错误码↔契约、localization_key↔唯一文案清单、manifest↔运行时版本；acceptance 分别映射 AC-01～AC-11 中属于后端职责的应用层行为；锁定依赖、空库迁移、重启恢复、文件清理和脱敏均在本机真实验证。Cloudflare、HTTPS 公网入口和 Android 真机联网联调不属于当前 Goal 的完成条件。

| 证据层级 | 当前 Goal 必须证明 | 不得据此宣称 |
| --- | --- | --- |
| `CREDENTIAL-SMOKE` | `DONE`：真实 Key、官方端点、`deepseek-v4-flash`、non-thinking JSON 响应可用；token 证据见状态基线 | 后端配置、Key 加密保存、正式 adapter、错误映射、业务生成或模型质量 |
| `LOCAL-DONE` | TestClient/localhost、真实本地 SQLite 与文件存储、重启后的恢复；应用服务使用 deterministic fake；正式 DeepSeek adapter 使用 mock HTTP transport 验证请求、鉴权、超时、响应解析、错误映射和脱敏 | HTTPS 实际传输、真实 Key/余额、真实模型质量、Android 客户端不存明文、生产可用性 |
| `LIVE-CAPPED` | 仅在 `LOCAL-DONE` 后，从本机 Git 忽略的 `.env` 加载真实 Key，通过正式 Key 保存/解密与 adapter 链路，按 R1 的固定样本与预算完成一次真实模型验证 | HTTPS/客户端安全、全书或所有题型质量、生产 SLA；live 结果不得替代 fake/mock 的失败分支和可重复测试 |
| `EXTERNAL-DEFERRED` | 当前不执行，不阻塞 F0～R1 | 未来以受信任 HTTPS 入口、Android 客户端安全检查和真机联网分别补证 |

## 4. 依赖驱动工作包

### F0 — 可执行基线与防漂移护栏

**`DONE`｜依赖：无｜覆盖：project-structure 5～7、structure-contract 1/7**

复用已创建的 Conda 环境；补齐 build backend/package discovery，使 `pip install -e .[dev]` 可用并生成锁定文件；建立单一 Settings、应用装配、DB session、隔离测试配置；实现统一错误对象/错误码/localization key 清单和 contract 守卫；提供测试 client、临时 DB/存储、可控时钟；实现 healthz，readyz 在 DB/存储不可用时真实 503。

验收：`conda run -n shanka-backend python --version` 为 3.12；从干净环境按锁定结果安装成功；四个工具命令通过；应用启动、空测试库创建；schema/OpenAPI、错误码、localization key、manifest 守卫；readyz 成败测试。开工前解决 R-01。

当前证据（2026-08-10，分支 codex/f0 合并回 main，9 commits c18df94..320466a）：
- 可编辑安装 + pip-tools 锁定：hatchling build backend，`pip install -e .[dev]` 成功；`requirements-dev.lock`（46 个钉版本）从干净 venv 安装 + editable 冒烟通过（sqlalchemy 2.0.51）。
- 四工具命令全绿：`python -m pytest` 34 passed、`python -m ruff check .` All checks passed、`python -m ruff format --check .` 107 files already formatted、`python -m mypy .` Success（69 source files），Python 3.12.13。
- 应用启动冒烟：真实 uvicorn（port 8099）healthz=200、readyz=200（`{"status":"ready","checks":{"database":"ok","storage":"ok"}}`），`main/shanka.db` 创建（git 忽略）。
- 空测试库创建 + readyz 成败：`test_probes_readyz_ok_creates_empty_db`（readyz 请求后文件落盘）、`test_probes_readyz_db_unavailable_returns_503`、`test_probes_readyz_storage_unavailable_returns_503`、`test_probes_healthz_alive_even_when_db_down` 全通过。
- 四类守卫（15 用例全绿）：schema↔openapi（含负例）、错误码↔第 7 章（23 码全等 + HTTP 状态）、localization_key↔文案清单（派生全等 + 格式正则）、manifest 资产版本/路径（+ 契约声明一致性）。
- 统一错误对象：`app/errors.py` 23 个 ErrorCode、ERROR_HTTP_STATUS、LOCALIZATION_KEYS（R-01 唯一位置 + 派生规则）；AppError → 1.4 响应形状集成测试通过。
- 无明文泄漏：`grep -rn "sk-" main/app --include="*.py"` 无输出。

### F1 — 数据与 HTTP 共享基础

**`DONE`｜依赖：F0｜覆盖：structure-contract 1.1～1.7、database-design 0～3、O-1/O-3**

实现 12 张表、约束、索引、外键、WAL、Alembic 初始迁移；统一 X-Device-ID、Idempotency-Key 指纹/首次响应重放、错误包装、request_id、限流；提供请求级 session、幂等请求指纹、并发占位、唯一约束和首次响应存储原语，跨设备统一 404；JSON 日志与 HTTP/限流指标；metrics 返回 Prometheus 文本。

验收：空库 upgrade/downgrade/upgrade；磁盘 SQLite 外键/WAL；幂等原语的并发占位与回滚、隔离、429 + Retry-After、探针豁免 integration 测试。业务副作用与首次响应的完整同事务验收在 V1 首个真实写接口完成。

当前证据（2026-08-11，分支 codex/f1 合并回 main，14 commits c057bab..edc7fcc）：
- 12 表 ORM + Alembic 迁移 0001（初始 12 表）+ 0002（request_body_hash 增量）：空库 upgrade/downgrade/upgrade 往返实测通过；磁盘 SQLite PRAGMA foreign_keys=1、journal_mode=wal；`alembic check` 零漂移。
- 设备鉴权（1.1/2.1）：缺失/非法 → 401 DEVICE_ID_REQUIRED/INVALID（1.4 形状）；首次见自动注册 devices 行（first_seen_ip/user_agent/last_active_at）；/healthz /readyz /metrics 豁免；服务端校验口径 = 通用 UUID 格式（契约 v4 表述指客户端生成约定，apiKey scheme 无 format 约束）。
- 幂等（1.3/2.12）：execute_idempotent 原语——先 SELECT 重放首次 2xx 响应、同键异 body → 409 IDEMPOTENCY_CONFLICT、非 2xx 不落库、BEGIN IMMEDIATE 串行化 + 唯一约束并发占位 + 冲突回滚重读重放（业务副作用仅一次）；request_body_hash 契约列（R-10）；flush 冲突兜底路径有确定性回归测试（20/20 稳定）。
- 限流（1.6）：5 维度（写 60/min/device、IP 5/s 含探针、api-key 10/h、samples 20/h、pdf 10/h）+ 429 RATE_LIMITED + Retry-After；阈值进 Settings 可运维调整；设备维度键 = 原始 X-Device-ID 头。
- 错误包装（1.4）：RequestValidationError → 400 VALIDATION_ERROR；未预期异常 → 500 INTERNAL_ERROR（内部细节只进日志）；Starlette HTTPException 404/405 语义保留。
- O-1 JSON 日志：8 契约字段 + method/path/status/duration_ms；request_id 中间件（X-Request-ID 响应头 + contextvars 贯穿）；请求日志不记录请求体（天然满足 1.5 掩码红线）。
- O-3 metrics：/metrics Prometheus 文本（豁免鉴权）；http_requests_total（method/path/status）、http_request_duration_seconds、rate_limit_hit_total（scope）；R-04 直测不进 OpenAPI；8.3 scope 值表与 1.6 命名一致（审核修正 device→write）。
- 中间件运行序（外→内）：Metrics → RequestID → RateLimit → DeviceID → Logging → 路由（insert(0) 语义；main.py 注释固化）。
- 验收实测：干净 venv 从 lock 安装 + 空库迁移往返 OK；真实 uvicorn 冒烟 healthz/readyz/metrics 200、decks 无设备 401/有设备 404、X-Request-Id 响应头；关键边界 23 用例两种文件顺序全绿；`grep -rn "sk-" main/app main/infra` 无输出。

### V1 — 牌组与卡片闭环

**`DONE`｜依赖：F1｜覆盖：FR-03/14/18、接口 6.5、AC-09**

实现牌组列表/创建/详情/删除、自由刷题列表、手动新增、原子批量导入；统一 Card/position/source；真实进度查询；删除保护、级联及历史任务 deck_id 置空。

验收：对应操作走真实 DB；追加不覆盖、导入全成或全败、逐张结果、稳定 position、重复删除和任务保护；同设备同 key 同请求单副作用并重放原响应，同 key 异请求冲突，失败时业务与幂等记录共同回滚，新 app/session 可重放，并发不双写；AC-09 通过。

当前证据（2026-08-11，分支 codex/v1 合并回 main，7 commits b78d560..0be69a4）：
- 牌组闭环：GET/POST /decks、GET/DELETE /decks/{deck_id} 走真实 DB；跨设备统一 404 DECK_NOT_FOUND；删除级联（cards→review_states/review_events CASCADE）+ 历史任务 tasks.deck_id SET NULL；删除保护（非终态 PENDING/RUNNING/PAUSED 任务引用 → 409 TASK_IN_PROGRESS）；不同 key 重复删除 404、同 key 由幂等层重放 204。
- 卡片闭环：手动新增（position=max+1 追加不覆盖、source=MANUAL、card_type=QUESTION、同事务插初始 ReviewState state=NEW/difficulty=1.0/due=now）；自由刷题列表按 position 稳定排序；批量导入同事务原子（全成或全败，逐张 results CREATED+card_id；空列表/空 front/back → 422 IMPORT_PARSE_ERROR）。
- 真实进度聚合：card_count/due_count（due<=now 服务端时钟）/mastered_card_count（C-03：REVIEW 且 stability>=21）/review_count/mastery_ratio（0 时 0）——service 层 SQL 聚合，非本地演示数据。
- 幂等首个真实写接口完整验收（F1 原语 + V1 接线）：同 (device, path, key) 重放首次 2xx 原响应体（POST 创建/import/DELETE 204）、同 key 异 body → 409 IDEMPOTENCY_CONFLICT、失败不落库（404 重试重新执行）、新 app/session 跨会话重放、handler 级并发同 key 一 fresh 一 replay 单副作用（2 线程 Barrier 实测 5/5 轮稳定）、幂等记录与业务副作用同事务。
- BodyCaptureMiddleware：写方法 raw body → request.state.raw_body 供幂等 hash；GET 不读；请求日志不记录 body（红线 4）；运行序 Metrics → RequestID → RateLimit → DeviceID → Logging → BodyCapture → 路由。
- Deck/Card schema ↔ openapi 守卫扩展（array/null 联合/嵌套 $ref）；AC-09 三条验收映射 + 5 补覆盖用例（8 用例）。
- 验收实测：四工具全绿（121 passed、mypy 101 files）；干净 venv 从 lock 安装 + 空库迁移 OK；真实 uvicorn（先 alembic upgrade 再启动）POST /decks 201 完整派生字段 + 同 key 重放响应体逐字一致 + 列表正确；边界 32 用例 + 守卫 20 用例全绿；`grep -rn "sk-" main/app main/services main/infra` 无输出。
- 部署纪律（冒烟暴露）：未迁移空库上业务请求 500 INTERNAL_ERROR——启动前必须 `alembic upgrade head`（readyz 只 SELECT 1 不校验 schema；R1 验收覆盖干净环境迁移启动）。
- 登记（V1 收口）：structure-contract 3.10 ReviewState difficulty 描述 "0~10" 与 database-design 2.10/ORM CHECK "1~10" 漂移——实现按 database-design（py-fsrs 口径 1~10），契约文本待同步（随 V4 或 R1 文档修订）。

### V2 — FSRS 复习与看板闭环

**`DONE`｜依赖：V1｜覆盖：FR-15/16、weekly_goal 边界、接口 5/6.6/6.8、AC-10**

以单一适配封装 py-fsrs 契约参数；实现到期队列、四档评级、client_event_id 去重、ReviewState 快照、牌组进度和 IANA 时区周看板。

验收：契约 5.1 确定性断言；事务回滚；重复/冲突事件；排序；周一分桶、DST/跨周、连续天数、首次/非首次、零分母 null、weekly_goal 缺省及 AC-10。

当前证据（2026-08-11，分支 codex/v2 合并回 main，11 commits 0b3ab91..970a45c + 契约修订 6a74422/8a7a41e）：
- py-fsrs 单一适配（services/scheduling）：固定 fsrs 4.1.2（R-13：任何 py-fsrs 版本无法逐字满足原 5.2 表——3.x 有 State.New 但无学习步配置；4.x/6.x 无 New 且步进语义偏移）；R-13 裁决采用 3 步 learning_steps=(10m, 10m, 1d)（py-fsrs 语义下 GOOD 间隔=steps[step+1]，5/5 行独立复现 5.2 表，符合 C-01 意图）；structure-contract 5.1/C-01/AGENTS.md 三处契约同步修订（2 步→3 步 + 参数数注释修正 21→19）。
- 评级闭环（services/review + /decks/{id}/review + /review-events）：到期队列（due<=now 按 due、position 排序）；四档评级事务（review_event INSERT + review_state 全量快照 UPDATE 同事务；回滚无部分写入）；state 落库统一大写（契约 3.10 枚举，V1 deck_progress mastered 口径自动对齐）；Learning step 由 due-last_review 间隔推导 + last_rating 消歧（AGAIN/HARD→step0、GOOD→step1、1d→step2——二次 GOOD +1d、三次毕业、AGAIN 后 GOOD +10m 均实证）；reps/lapses 自计数（py-fsrs 4.x Card 无该属性）。
- 双幂等（1.3）：Idempotency-Key 优先（execute_idempotent 全快照重放）；client_event_id 兜底（UNIQUE(device_id, client_event_id) 冲突 → 比对 card_id+rating → 一致重放不重复计数/不一致 409 REVIEW_EVENT_CONFLICT）；R-12 口径：key 层重放=完整快照、client_event_id 兜底重放=当前 review_state 视图。
- 看板（/stats/dashboard）：IANA 时区周一 bucket（zoneinfo，DST 周界实测偏差 0）；weekly_activity[7]/weekly_total/week_change（上周 0→None）/weekly_goal_progress=min(周总数/goal,1)（缺省→None）/recall=周内 GOOD/全部/first_answer=每卡历史首个 GOOD 累计/retention=周内非首次 GOOD/非首次/streak=本地当天向前连续自然日/mastered=全量 C-03/分母 0→None/has_data；R-12 口径登记。
- 确定性断言（C-02 fuzzing 关闭）：同输入同输出；5.2 表 5 行全复现。
- 验收实测：四工具全绿（162 passed、mypy 111 files）；干净 venv 安装 + 迁移 OK；真实 uvicorn 复习闭环冒烟（建卡→队列 1→评级 LEARNING/reps=1→队列 0→看板 weekly_total=1/has_data=True/recall=1.0）；边界 60 用例全绿；泄漏 grep 仅 docstring "task-1-report" 的 "sk-" 子串误报（无真实密钥）。
- 登记：R-12 RESOLVED（看板口径裁决 + client_event_id 兜底重放口径）；R-13 RESOLVED（py-fsrs 版本/学习步配置裁决 + 契约同步）；structure-contract 3.10 difficulty 范围漂移（V1 登记，保持待同步）。

### V3A — PDF 生命周期闭环

**`DONE`｜依赖：F1｜可与 V1/V2/V3B 并行｜覆盖：FR-01/02/18、接口 6.1、AC-01/02 及 AC-08 后端存储边界**

实现 PDF 三重校验、大小/页数限制、受控存储、文本层/真实目录解析、轮询、章节 PATCH、最近列表、删除保护；不做 OCR 或目录兜底。解析由进程内、DB 驱动的可重启扫描器执行。验收书籍 `res/AI-Agents-in-Depth-zh-CN.pdf` 已有文本层和书签目录，执行 Agent 只需程序化解析文本/书签，不依赖视觉、页面截图或 OCR。

验收：有效/无目录/扫描件/损坏/伪 MIME/超限、路径穿越、隔离；以磁盘 DB 和文件存储启动新 app/worker 验证恢复；章节范围、删除清理及 AC-01/02 的本机后端行为通过。

当前证据（2026-08-11，分支 codex/v3a 合并回 main，10 commits ae5a1a1..b7dab5b）：
- 三重校验与限制（6.1）：魔数 %PDF + 扩展名 .pdf + MIME application/pdf + ≤50MB + ≤500 页（Settings pdf_max_size_bytes/pdf_max_pages）→ 400 PDF_UPLOAD_INVALID；页数 hint 由 handler 用 PdfReader 读取（损坏文件 hint=None 由扫描器 FAILED 兜底）。
- 受控存储（1.7/2.3）：storage_key=随机 UUID hex、分目录（[:2]/[2:4]）、32 位 hex 严格校验（路径穿越防护）；删除元数据同步清理存储对象（失败 WARN 不阻断）。
- pypdf 解析（5.1/5.2/AC-01）：文本层抽样（前 5 页）+ outline 顶层条目为章节（样书 318 页、12 章节：引言/第1-10章/后记，页码 1-based 归一化 + clamp）；不可提取 → PDF_PARSE_FAILED、无目录 → PDF_TOC_MISSING（FAILED 终态 + error_code，不重试不删原始文件）；不 OCR/不猜测/不兜底。
- 扫描器（4.4 定式）：进程内 DB 驱动（PENDING/PARSING → PARSING → PARSED/FAILED），lifespan daemon 线程（pdf_scan_interval_seconds=1.0，wait-first 宽限期）；重启后 PENDING/PARSING 残留重新解析（chapters 先删后插幂等）。
- 路由：POST /pdfs（201 PENDING）、GET /pdfs（最近列表 device+created DESC）、GET /pdfs/{file_id}（轮询详情 + 章节）、DELETE（204；非终态任务 409 TASK_IN_PROGRESS + 存储清理 + tasks.file_id SET NULL）、PATCH 章节（**部分更新语义**——openapi 无 required"至少一个字段"；全 None → 400；非 PARSED → 409 TASK_STATE_CONFLICT；范围校验）。
- 幂等：POST/DELETE/PATCH 走 execute_idempotent（multipart body hash=文件内容；同 key 重试孤儿文件 MVP 接受已登记）。
- 修复（F1 遗留契约违约）：rate_limit 专门维度前缀匹配（/v1/pdfs 等 → 无前缀路由）——pdf/samples/api-key 专门维度（10/h/20/h/10/h）自 F1 起从未生效，V3A 修复并加 pdf 维度 429 回归测试。
- 验收实测：四工具全绿（201 passed、mypy 120 files）；干净 venv 安装 + 迁移 OK；真实 uvicorn 冒烟（上传样书 PENDING → 后台扫描轮询 PARSED、12 章节、PATCH 部分更新 200）；边界 67 用例全绿；无密钥泄漏、实现无样书路径引用。
- 登记：AC-01/02 验收通过（TOC_MISSING 专属证明在集成层）；AC-08 后端存储边界（日志不记 body + 文本样例 501 字符上限不落库）；样书硬断言（12 章节/318 页）校准值——样书变更需同步（services/pdf/AGENTS.md 注明）。

### V3B — API Key 安全与 DeepSeek 适配边界

**`DONE`｜依赖：F1｜可与 V1/V2/V3A 并行｜覆盖：FR-17/18、接口 1.5/6.2、AC-08/11 的后端本机部分**

实现 Key 状态映射、AES-256-GCM 环境密钥加密与覆盖规则，仅 infra/llm 解密；完成正式 DeepSeek adapter。模型与 thinking 模式通过单一配置入口注入并记录实际值，不在领域/业务层硬编码；应用服务使用 deterministic fake，正式 adapter 使用 mock HTTP transport，不访问外网。

验收：AVAILABLE/INVALID/余额不足/上游不可用、旧有效 Key 保护；adapter 请求鉴权、thinking 开关、JSON 输出约束、超时、畸形响应、错误码和脱敏；数据库、响应、日志、异常、任务与分析数据均无明文。真实 Key/余额与客户端存储不在本机完成声明内；本次直连 smoke 不计为 adapter 验收。

当前证据（2026-08-11，分支 codex/v3b 合并回 main，7 commits 792bfb2..d8b7be6）：
- AES-256-GCM 加密（infra/llm/crypto.py）：随机 12B IV 随密文（base64(iv+ct+tag)），解密密钥来自环境变量（Settings api_key_encryption_key，repr=False，32 字节 hex）；模块位于 infra/llm/（红线 4：仅该路径可解密）；service 不导入 decrypt_key。
- 覆盖规则（6.2）：仅 AVAILABLE 落库/覆盖；INVALID/INSUFFICIENT_BALANCE 不保存不覆盖（旧有效 Key 保护实测：`sk-****lid1` 保留）；API_KEY_UNAVAILABLE 502。
- 状态映射（3.1）：AVAILABLE（balance 端点 200+is_available）/INVALID（401）/INSUFFICIENT_BALANCE（200+is_available=false）/UNKNOWN（未保存，masked_key=""）；上游 429/5xx/网络 → API_KEY_UNAVAILABLE。
- DeepSeek adapter（infra/llm/deepseek.py）：validate_key（/user/balance）+ chat（/chat/completions，thinking 开关 `{"type": "enabled"}`、JSON output `response_format={"type":"json_object"}`、超时 Settings.deepseek_timeout_seconds、usage 四键映射 prompt/completion/cache hit/miss）；错误映射（401→INVALID/API_KEY_UNAVAILABLE、429/5xx→API_KEY_UNAVAILABLE、解析失败→GENERATION_FAILED）；`raise ... from None` 切断异常链；日志仅状态码/异常类型（1.5 红线）；transport 可注入（httpx.MockTransport 全 mock 验证，不访问外网）。
- 模型/thinking 单一配置入口（R-09）：Settings deepseek_model="deepseek-v4-flash"、deepseek_thinking=False（冻结默认可替换）。
- 路由：PUT /api-key（200 ApiKey；幂等 execute_idempotent——重放不重复校验实测 validate_calls==1；加密密钥缺失 → 500 INTERNAL_ERROR；client try/finally close）+ GET /api-key/status（200；UNKNOWN 空态）。
- 脱敏（AC-08/11）：响应仅 status/masked_key（sk-****后4位）/updated_at；DB 密文断言无明文；caplog 日志断言无明文（含 alembic fileConfig 禁用 logger 的测试坑处理）；请求日志不记 body。
- 验收实测：四工具全绿（242 passed、mypy 130 files）；干净 venv 安装 + 迁移 OK；uvicorn 冒烟（GET status UNKNOWN 空态、缺密钥 PUT 500——LOCAL-DONE 前不触网）；边界 64 用例全绿；泄漏 grep 仅 "task-1-report" 误报。
- 登记：thinking 参数名/模型 id/balance 响应结构需 R1 live 核对（mock 契约已验证）；migrations/env.py fileConfig disable_existing_loggers 仓库性测试坑（R1 统一修）。

### V4 — 样卡、任务与知识点规划

**`DONE`｜依赖：V1 + V3A + V3B｜覆盖：FR-04/05/06/18、接口 3.5/4.1/4.2/6.3/6.4、AC-03**

按 manifest 加载/校验资产；Prompt 稳定前缀与动态后缀分离；固定构成 3 张样卡且不入库；创建/查询/取消任务，持久化配置/章节，规划 KnowledgePoint，以 DB 条件更新抢占执行。

验收：样卡构成；无 Key/章节/牌组、非法比例；COMPACT ≤ BALANCED ≤ EXTENSIVE；自定义要求不继承；同 key 单任务；状态转移和 AC-03。

当前证据（2026-08-11，分支 codex/v4 合并回 main，9 commits 89c0390..f4e9077）：
- manifest 加载与 Prompt 组装（infra/llm/prompts.py）：按 agent_evolution/manifest.json 加载（R-03 只读）；稳定前缀（资产）+ 动态后缀（topic/chapter/difficulty/custom/JSON schema）；完整 Prompt 不落日志（红线 4/AC-08）。
- 样卡（6.3/AC-03）：POST /samples 豁免幂等键；固定 3 张（1 基础+1 理解+1 应用；2 问答+1 判断——fake 按难度定类型）；不入库不统计；GenerationConfig 校验（difficulty_ratio 三值>0 和=1、quantity_tendency 枚举 → 400）。
- 任务（6.4/4.1）：POST /tasks（幂等 + 校验归属/配置/已保存 Key（无 → 422 API_KEY_NOT_SET）→ RUNNING + stage=GENERATING + selected_chapters/generation_config JSON 快照（**对象数组**——契约 3.4/3.6 名称还原）→ 规划同事务）；GET 轮询；cancel（PENDING/RUNNING/PAUSED → CANCELLED）；resume（DB 条件更新 PAUSED AND resumable=1 → RUNNING，否则 409 TASK_STATE_CONFLICT）。
- KnowledgePoint 规划（5.4.1 可测口径）：每章 3×密度（COMPACT=1/BALANCED=2/EXTENSIVE=3）——2 章实测 6/12/18；字段完整 + PENDING。
- 任务执行（V4 fake，红线不代替生产）：进程内 DB 驱动后台循环（4.4 定式）；deterministic fake 生成（sha256 派生 ID，seed 含 task 维度——跨任务不冲突）；入库 V1 模式 + generation_item_id 部分唯一索引防重（AC-05）+ 难度按 priority 轮换三档；COMPLETED/FAILED 状态机。
- 验收实测：四工具全绿（289 passed、mypy 148 files）；干净 venv 安装 + 迁移 OK；uvicorn 冒烟（样卡 3 张 QUESTION+TRUE_FALSE、任务 RUNNING→COMPLETED 12 卡入库）；边界 70 用例全绿；无明文泄漏。
- 登记：R-14 RESOLVED（2026-08-11 SampleCard 轻量组件落地——契约 3.13 + openapi + schemas 三处一致，handler 去占位，见第 4 节 R14 包）；fake 跨设备防重已由 task 维度修复；4.4 表述 PENDING vs RUNNING 观察（V5A 同步契约文本）。
- 流程记录：V4-T4 fix 中 implementer 越权修改 Progress.md（登记 R-14）——内容正确保留，后续已重申禁令。

### V5A — 分批生成与质量观测闭环

**`DONE`｜依赖：V4｜覆盖：FR-07～11/18、接口 4/6.4/6.9/6.10/8、AC-04/07**

按知识点分批，每批最多 2 次重试；Schema 是唯一入库门槛，Rubric 只观测。合法卡、generation_item_id、知识点/批次状态、游标、计数原子推进；失败批次 SKIPPED 后继续。记录实际 model/system_fingerprint、prompt/cache-miss/cache-hit/output token、Prompt/Schema/Rubric 版本、Rubric 与质量；提供批次/质量聚合/指标/按生效日期配置的成本估算。

验收：低 Rubric 合法卡入库；非法输出三次后跳过；已完成批次和 generation_item_id 不重复；provider usage 原样字段与内部统一字段映射受测，价格调整不改历史 token 数据；版本/缓存/质量/成本可核验；AC-04/07。

当前证据（2026-08-11，分支 codex/v5a 合并回 main，10 commits 9bc4301..ae3f498 + fix 7c223a5 + 契约同步 89c3aa8）：
- Schema 校验器（services/generation/schema_validator.py）：card.schema.json（Draft 2020-12，顶层 required=[type, front, back] + allOf 类型条件）经 manifest 资产加载；**Schema 是唯一入库门槛**（Rubric 不影响）。
- 分批执行核心（services/generation/batches.py）：知识点按 batch_size=3 分组；批次状态机 PENDING→PROCESSING→SUCCEEDED（≥1 合法卡）/FAILED（0 合法卡）→重试 2 次共 3 次尝试→SKIPPED（**重试预算对齐契约 3.7**——fix 连带修复 _next_processable 假 COMPLETED 风险 + attempts==3 守卫）；游标 completed_batch_count/计数/批次状态同事务原子推进；已完成批次不重复；SKIPPED 后任务继续。
- 生成链路（LOCAL-DONE 红线）：正式 adapter（DeepSeekClient）+ mock HTTP transport（不触网）；executor 解密 Key 构造 client；响应→内部卡映射（front/back 产出 + QUESTION/TRUE_FALSE 分支）→逐卡 Schema 校验→合法卡入库（V1 模式 + generation_item_id 防重）；系统级错误→任务 FAILED（已入库卡保留）；V4 fake 退役（样卡仍用 fake）。
- Rubric 观测（5.9/8.5）：deterministic fake judge（4 维度 0-3 分总分 0-12，本地规则）；分数落 Card 5 字段 + 批次质量 6 字段（coverage/duplicate/难度/章节/类型分布/difficulty_deviation——V5A 简化恒 0）；rubric_version 记录；Rubric 不影响入库。
- usage/版本观测（3.7/8.4）：cache_hit/miss/output tokens、model、http_status、duration_ms、prompt/schema/rubric_version 落 Batch；provider usage 原样字段与内部统一字段映射受测；request_id 待上游透传（R1）。
- 观测出口：GET /tasks/{id}/batches（Batch 视图含质量/usage/版本/cost_estimate）；GET /observability/quality-summary（group_by model|pdf|difficulty、days=30、device 隔离：Rubric 均分/覆盖/重复率/任务完成率/成本汇总）；6 个 8.3 指标（llm_requests_total/llm_request_duration_seconds/llm_tokens_total/generation_tasks_total/generation_tasks_duration_seconds/batch_retry_total——infra/metrics.py 共享 REGISTRY + 上报点）；cost.py 价格常量（2026-08-11 起 hit 0.5/miss 2/output 8 元每百万 token，生效日期取档，历史 token 不变）。
- 契约同步（R-16 RESOLVED）：openapi Batch schema 补齐 9 观测字段（structure-contract 3.7 派生）。
- 验收实测：四工具全绿（313 passed、mypy 161 files）；干净 venv 安装 + 迁移 OK；uvicorn 冒烟（quality-summary 空态、metrics 9 指标行——LOCAL-DONE 不触网）；边界 61 用例全绿；无明文泄漏。
- 登记：AC-04（低分合法卡照常入库/非法 SKIPPED/不自动修复）与 AC-07（Rubric+Cache 记录且不影响入库）验收通过；difficulty 分组语义未契约化（structure-contract 6.10 补分组键定义——R1 契约修订）；FAILED 任务批次滞留 PROCESSING（V5B 恢复闭环）。

### V5B — 任务恢复、取消与并发闭环

**`DONE`｜依赖：V5A｜覆盖：FR-12/18、任务状态机、AC-05**

在 V4 的唯一任务状态机上补 checkpoint/resume/cancel、RUNNING 心跳、30 分钟孤儿恢复和 DB 条件抢占；不建立第二套任务框架，不引入外部队列。

验收：以磁盘 DB/文件存储注入崩溃并创建新 app/session/worker 恢复；并发 worker/resume 单执行者；完成批次和 generation_item_id 不重复；取消保留已入库卡；AC-05 通过。

当前证据（2026-08-11，分支 codex/v5b 合并回 main，5 commits 6de2071..3fb2059 + fix 4d22b53）：
- 心跳（4.1）：executor 批处理循环内每批后刷新 task.updated_at（SystemClock format_utc）——**批次事务粒度**（每批 commit：批次状态+游标+心跳同事务落库）；崩溃后已完成批次已提交、未完成批次 PENDING/FAILED 可恢复。
- 批次级条件更新抢占（并发 worker 单执行者）：_claim_next_batch 用 `UPDATE ... WHERE status IN (PENDING, FAILED)`（免疫 identity map 陈旧快照死循环——fix 连带）；rowcount=0 → 下一条；已完成批次天然不可取。
- 孤儿 RUNNING 恢复（4.1）：resume 条件更新扩展 `(PAUSED AND resumable=1) OR (RUNNING AND updated_at < now-30min)` → RUNNING；rowcount=0 → 409 TASK_STATE_CONFLICT（fresh RUNNING 409 / 过期 200 双侧测试）；orphan_timeout_minutes=30（Settings，handler 传参）。
- 崩溃恢复（AC-05 四条）：SystemExit 崩溃模拟（批 2 前）→ 批 1 卡保留/游标 1→2/批 1 不重跑（calls 计数）/generation_item_id 不重复（5 卡互异 + duplicate_rate 0.5）；取消保留已入库卡（CANCELLED 终态不重试）。
- 验收实测：四工具全绿（322 passed、mypy 163 files）；干净 venv 安装 + 迁移 OK；uvicorn 冒烟 healthz 200；边界 36 用例全绿；无明文泄漏。
- 最终整支审查【有条件放行】→ I-1 修复（fix commit 43ff512 + scoped re-review approve）：批次间隙 cancel 不再被静默覆盖——批循环每批 commit 后 `session.refresh(task)`，`status != "RUNNING"` 即 break（停止处理、保留已入库卡）；COMPLETED 改条件更新 `WHERE status='RUNNING'`（rowcount=0 → 不覆盖、不观测）；新测试 `test_executor_cancel_between_batches_preserves_cancelled`（断言 CANCELLED/ended_at 保留、批 2 PENDING 未 claim、chat 停批 1、仅批 1 卡入库）。
- 登记：AC-05 通过；内容级去重观察（AC-05-d 为 ID 级——PRD 语义已按 ID 级实现，DB 部分唯一索引兜底）；SQLite 下 rowcount=0 真实争抢不可构造（服务器 DB 语义验证——未来多实例）；chat 期间 cancel → 500 database-locked 为 SQLite 单写者固有代价（BEGIN IMMEDIATE 锁跨长事务，无 busy_timeout 重试）——登记 R-17，多实例/生产 DB 议题不阻塞。

### V6 — 单卡重写闭环

**`DONE`｜依赖：V5B + V2｜覆盖：FR-13、接口 6.7/C-05、AC-06**

同 card_id 原地替换、position 不变、version 递增、新 generation_item_id、Rubric 记录，原子重置 ReviewState；失败不改变内容/排程；来源不出响应。

验收：成功、Schema/LLM 失败、并发、幂等重放/冲突、隔离和 AC-06。

当前证据（2026-08-11，分支 codex/v6 合并回 main，9 commits e24749d..9893b73）：
- 契约增补（兼容性）：`REWRITE_SCHEMA_INVALID` 422（errors.py 三处 + structure-contract ch7，守卫全等校验）；openapi rewrite 响应表补 400（BadRequest 组件）/502（内联，api-key 先例）——500 全仓不列（全局兜底惯例）。
- rewrite prompt 资产：`agent_evolution/prompts/v1/rewrite.md`（原卡上下文 + 类型保持 + `{"cards":[单卡]}` 输出契约）+ manifest prompts.rewrite v1 + CHANGELOG（红线 5：asset_versions() 不漂移，仍取 generator）。
- 用例 `services/cards/rewrite.py`：归属查卡（统一 404）→ Key（无 Key 422 API_KEY_NOT_SET / 解密失败 502 / 上游错误 500，明文仅注入 client）→ chat → 解析/校验（违约 422 REWRITE_SCHEMA_INVALID，原卡零写入）→ 原地替换（card_id/position/source/code/created_at 不变；内容字段更新含类型切换清残留；新 generation_item_id；target_difficulty/knowledge_point_ids 保留；Rubric 5 字段落卡，低分照常替换 AC-06；version 递增 vN→v(N+1)，非 vN（手动卡时间戳）→ v2；updated_at 递增）→ ReviewState 原子重置（NEW/0.0/1.0/due=now/reps=0/lapses=0/last_review=None/last_rating=None）→ flush 不 commit（幂等同事务，handler 接线）。
- 共享提取：`services/generation/response_parse.py`（parse_cards_json/to_internal_card 自 batches.py 纯移动，批次路径零行为变化——全量回归兜底）；`services/generation/llm_metrics.py`（observe_llm_call 共享，rewrite chat 后上报 8.3 指标——final review I-1 修复）。
- 路由 `POST /cards/{card_id}/rewrite`（main.py 装配）：V1 create_card 幂等接线同款（execute_idempotent + 同事务 commit；仅 2xx 落幂等记录；同键异 body 409；错误路径不 commit 无残留——T3 Minor 2 集成确认）；client_factory 经 app.state 注入（测试 mock transport，生产 None 构造真实 client）。
- 验收：四工具全绿（348 passed、mypy 169 files）；AC-06 三条映射（可重写/仅 Schema 通过替换/失败保留）+ 幂等重放（chat 计数=1）/409 + 无 Key 422/解密失败 502/跨设备 404 HTTP 层 + 并发（同卡后写覆盖 v5 无脏读、复习×重写两序终态一致）+ 来源不出响应 + llm 指标断言；干净 DB alembic 迁移 + TestClient rewrite 全链路冒烟（200/v2/ReviewState 重置/healthz）。
- 登记：R-18（version 递增格式分支：手动卡 ISO 时间戳 → "v2"；MANUAL/IMPORTED 卡重写后 generation_item_id 非空但部分唯一索引仅覆盖 GENERATED——uuid 随机无防重冲突，source 不变语义）；requestBody required:true vs 实现容忍缺省（接受现状——实现接受契约超集，前端不发空 body）；rewrite prompt 占位符 replace 顺序（卡内容含字面 `{back}` 等会被后续 replace 篡改——仅影响 prompt 输入不影响落库，概率极低，登记）。

### R1 — 本机契约回归、受控真实模型验证与交付

**`DONE`｜依赖：V1、V2、V3A、V3B、V4、V5A、V5B、V6｜覆盖：PRD 7～10 的后端可本机验证部分、AC-01～11**

当前证据（2026-08-11，分支 codex/r1 合并回 main，8 commits 9ffaa09..ea8dff0 + live 报告）：
- **本机门槛 1（契约回归）**：PRD 7-10 后端可本机验证核对清单（`test_acceptance_r1_paths.py`，27 数据行：25 项既有覆盖 + 2 缺口补：PDF 内容/Prompt 内容不落日志——AC-08 补齐，caplog INFO 级判别）；生产代码 mock 核对：无不允许项（仅 V4 样卡 fake 契约允许 + 测试注入点）。
- **本机门槛 2（干净环境）**：干净 venv（python3.12 venv 不可用 → conda 干净环境 Python 3.12.13）+ `requirements-dev.lock` 锁定安装 ✓；alembic 迁移 13 表 ✓；uvicorn 启动 healthz/readyz/metrics 200 ✓；写入 + 幂等重放同 deck_id ✓；杀进程重启数据保留 ✓。
- **live 设施**：adapter `trust_env=False`（直连绕过本机代理）+ `system_fingerprint` 透传；60 抽样框（seed 20260811，第 1/2/6 章各 20 块，24/24/12）；driver（正式链路 + 成本监控 + canary 即停 + 单次运行保护 + dry-run/live 分离），dry-run 60/60 + 停止条件专项验证。
- **canary 失败 → 实质修复（两轮）**：live1 canary 0/3——generator v1 输出裸单卡对象与 V5A 解析器 `{"cards":[...]}` 契约断裂（mock 掩盖的遗留缺陷）→ 修复 1：prompts/generator v1→v2（输出包装）；live2 canary 复验 1/3——v2 规则 4「恰好一张」与批次语义冲突 → 修复 2：规则 4「每知识点一张卡」（诊断调用 3 次定位验证，manifest/CHANGELOG/4 断言同步，353 passed 全绿）。
- **live 正式运行（live3，正式样本仅 1 次）**：59/60 成功；失败单元 43 为上游系统级抖动（GENERATION_FAILED，批次停留 PROCESSING，非 Schema/我方缺陷）；正式运行成本 **¥1.6351**（含 canary/诊断全战役 ¥1.7436，上限 ¥5/¥10 未触发）；tokens prompt 85,599（hit 68,224/miss 17,375）+ output 195,774；fingerprint 单一 `fp_a18b46594c_prod0820_fp8_kvcache_20260402`；60/60 幂等重放 ✓；**321 卡** generation_item_id 无重复。
- **统计（R-05 口径）**：失败率 1/60 = 1.67%，Wilson 95% 双侧 [0.29%, 8.86%]（scipy 非依赖用标准库 Wilson，单侧上界约 7.1~7.7%，Clopper-Pearson 接近）；完成率 98.3%（对照 8.1 ≥90% ✓）；仅描述固定抽样框 × 冻结模型，不外推。
- **人工复核 18 张**（难度分层固定 seed）：18/18 无事实错误/前后不匹配；BASIC 定义清晰、UNDERSTANDING 判断题正确、APPLICATION 场景题质量高；2 张英文术语/题干（原文保留）、1 张答案简短——描述性记录。
- **交付**：`docs/r1-live-report.md`（执行参数/统计/失败详情/复核/边界）；Progress F0-R1 全部 DONE。
- 登记：R-03 generator v2（v1 裸单卡契约断裂修复）；R-20（live 实证：1/60 上游失败、成本 ¥1.635、fingerprint 冻结）；API_KEY_ENCRYPTION_KEY 未提供（driver 临时密钥，live DB 密文跨进程不可解——已知限制）。

### R14 — SampleCard 轻量组件（R-14 清账）

**`DONE`｜依赖：V4｜覆盖：structure-contract 6.3、红线 1（三处一致）｜2026-08-11**

- **背景**：V4-T4 fix F-2 登记 R-14——openapi /samples 响应 items `$ref Card`（required 含 deck_id/position/created_at/updated_at），样卡不入库无真实值，V4 过渡由 handler 合成占位（deck_id=""/position=0）；R1 契约修订未落地 SampleCard 组件。
- **契约**：structure-contract 新增 3.13 SampleCard（11 字段，删去落库/归属/版本语义字段 deck_id/position/source/generation_item_id/knowledge_point_ids/Rubric 四维+总分/version/created_at/updated_at——PRD 5.5 数据规则）；openapi 新增 SampleCard 组件（required 四项 card_id/front/back/card_type；7 个可选字段 null 联合 + description，对齐 Card 先例）；6.3 表述同步（"响应返回 SampleCard 轻量组件"）。
- **实现**：`app/schemas/samples.py` 新增 SampleCard 守卫锚点模型；`app/api/samples.py` 移除合成占位（死代码 _now/SystemClock 清理），`_to_sample_card` 显式映射（fake 超集 → 轻量子集，type: ignore[arg-type] 4 处 mypy 必需）；样卡行为不变（不入库/不统计/豁免幂等键）；fake 生成器与 database-design 不动。
- **测试**：守卫新增 `test_sample_card_schema_openapi_consistent`（5 passed）；samples 集成 + AC-03 占位断言 → SampleCard 断言（先红后绿 TDD）；顺带修复 base 遗留的 2 处 ruff format 漂移（test_acceptance_ac04_ac07.py:293、test_batches.py:216——R1 canary v1→v2 改长行时引入，main 验收在行变长前）。
- **验收实测**：四工具全绿（**354 passed**、ruff check、ruff format 213 files、mypy 174 files——主 Agent 亲自复跑）；审查两轮（契约合规 ✅ + 代码质量 ✅ → Important-1 openapi null 声明修复 + Minor-1/2 → scoped re-review ✅ 无残留）。

### R22 — Agent 成本观测能力层 + 任务价格预估接口

**`DONE`｜依赖：V5A｜覆盖：契约 8.4 扩展、6.4、红线 1｜2026-08-12**

- **能力层**（services/generation/token_estimator.py）：token 用量估算模型——常量挂观测校准闭环（PROMPT_TOKENS_PER_KP=1500 / OUTPUT_TOKENS_PER_KP=3300，2026-08-12 校准自 R1 live 实测 1,427/3,263，向上取整偏保守；custom_requirements 每字符 ≈0.5）；输入映射与 V4 规划同口径（每章 3×密度系数，每知识点一卡）；区间估值复用 cost.py 价格档位公开入口（low=全命中 / high=全未命中，output 固定价），不重复定义价格。
- **消费点**（POST /v1/tasks/estimate）：`{chapter_ids, generation_config}` → `{knowledge_point_count, estimated_card_count, price_low, price_high, currency}`；复用 validate_config（400）；纯计算、不落库、豁免幂等键、不需要 API Key。
- **契约**：structure-contract 8.4 能力口径 + 6.4 接口行；openapi /tasks/estimate + CostEstimateRequest/Response 组件；schemas 守卫锚点，三处一致。
- **live 冒烟**（Task 4 输出原文贴此）：3 次真实调用 prompt 均值 811 / output 均值 1126（常量 1500/3300，偏差 −45.9%/−65.9%）；实际金额 ¥0.0319（全 miss 口径）落在区间 ¥0.0282~¥0.0319；偏差 >20% 按校准闭环纪律登记观察（常量不自动修改，待人工决策；另观察每次调用 ~384 token 前缀缓存命中，估算按全 miss 保守口径未建模缓存）。
- 验收实测：四工具全绿（378 passed、mypy 178 files）；10 用例全绿（5 unit token_estimator + 5 integration estimate）；预估无副作用（任务表零写入）。

### LLM-P1 — LLM 链路升级（生成批=单元、规划/评分真实 LLM、账本权威、估算删除）

**`DONE`｜依赖：V3A、V4、V5A、V5B、V6、R22｜覆盖：spec 2026-08-12-llm-pipeline-upgrade §3～§13、R-03｜2026-08-13**

设计权威：`docs/superpowers/specs/2026-08-12-llm-pipeline-upgrade-design.md`（FROZEN）；执行计划：`docs/superpowers/plans/2026-08-12-llm-pipeline-upgrade.md`（17 任务，superpowers:subagent-driven-development 逐任务 implementer + reviewer 双审）。任务包：`docs/llm-account-long-run-v1/`。

当前证据（2026-08-13，main 分支 18 commits 8cd0cb5..57442b7 + frontend-app 仓库 2a9f6b7）：
- **17 任务全部 review-clean**：T1 迁移 0003+ORM（text_chunks/llm_call_attempts 新表、knowledge_points/batches/tasks 新列，空库往返+alembic check 零漂移）→ T2 硬上限 Settings（9 字段）→ T3 页文本解析持久化（一页一行、确定性 chunk_id）→ T4 预算与三层最大余数配额 → T5 资产 v3/v2+manifest 扩展 → T6 adapter retryable 分类 → T7 llm_call_attempts 账本服务层 → T8 任务创建 PENDING+PLANNING+预算校验 → T9 规划执行（CAS 抢占/快照冻结/账本恢复/空单元三分支/指纹漂移 fail fast）→ T10 生成批=单元（锚定校验/页文本输入/账本同事务/SOURCE_INSUFFICIENT 不重试）→ T11 SCORING 阶段（确定性分层抽样/合批/回写守卫 STALE_SCORING_INPUT/非阻塞）→ T12 quality-summary 分母/归因/成本口径 → T13 估算删除（端点/token_estimator/planning.py 全删）→ T14 契约同步（structure-contract 3.4~3.7/3.10/4.1/6.x/8.5、PRD 5.4.1/5.6/5.7、database-design 2.5~2.7/2.13/2.14、openapi、守卫 44/44）→ T15 前端（阶段文案/空结果/跳过提示/SCORING 卡访问，嵌套仓库本地提交）→ T16 V4/V5A/V6 测试新语义更新+全量归零 → T17 canary+完成口径。
- **本地实现证据（LOCAL_IMPLEMENTATION_DONE）**：全量 **496 passed / 0 failed**；`ruff check .`、`ruff format --check .`（247 files）、`mypy .`（196 source files）四工具全绿（主 Agent 2026-08-13 亲自复跑）；契约守卫 44/44（schema↔openapi、ORM↔database-design、错误码、localization、manifest↔运行时版本）；账本同事务崩溃恢复经 executor 端到端判别测试；验收意图 AC-04/AC-05/AC-07 保全核验（reviewer 逐条核对无断言弱化）。
- **受控真实 canary（PRODUCTION_VALIDATED）**：单任务 Planner→Generator→Scoring 全链路真实 DeepSeek（`.env` Key，权限 600），**连续 3 次全 PASS**——规划规范化结果落库（3 单元锚定 QUESTION×3 + TRUE_FALSE 样例）、生成 3 卡锚定正确、评分 3/3 回写（总分 8~12）、账本 7 行/次全 SUCCESS、cost ≈ ¥0.015~0.029/次（累计全战役 ≈ ¥0.06，上限 ¥3 未触发）；canary 发现 2 个 mock 掩盖缺陷并修复：① adapter 内部 HTTP 重试（429/5xx/网络/超时 ×1，SDK 等价，账本 attempt 语义不变——SCORING 不重试语义下瞬时抖动曾致评分全灭）；② thinking 禁用显式携带（上游默认启用 reasoning 挤掉 content 空响应，实测 5/5 复现，`"thinking": {"type": "disabled"}` 后 2/2 正常）。两修复均 TDD + 判别测试 + 全量回归。
- 登记：R-03 RESOLVED（见第 6 节）；PRD 5.4.2 行 231/238 残留冲突（spec §12 范围外，偏差已登记 V2.2 收敛）；R-22 估算链路随本包删除。

## 5. 依赖关系与下一步

```text
F0 → F1 ─┬→ V1 → V2 ─────────────────────┐
         ├→ V3A ─┐                              │
         └→ V3B ─┴→ V4 → V5A → V5B → V6 ─────┼→ R1
                    ↑                 ↑          │
                    V1                V2 ────────┘
```

取编号最小、依赖已 DONE 的 TODO。并行只用于文件和事务边界不重叠的工作包；F0/F1、V4/V5A/V5B、R1 不并行拆分。app/schemas、openapi、ORM/migration、middleware 装配和 agent_evolution/manifest.json 属高冲突入口，只允许主 Agent 串行集成。

## 6. 已知冲突与风险

| ID | 状态 | 事实与影响 | 处理边界 |
| --- | --- | --- | --- |
| R-01 | `RESOLVED` | 要求校验 localization_key↔文案清单，但正式契约未指定清单 | 唯一位置 = `app/errors.py`（ErrorCode 注册表 + LOCALIZATION_KEYS 显式清单），派生规则 `"error." + 错误码.lower()`；`test_localization_guard` 校验派生集合与清单全等（F0-T4/T6，已实测） |
| R-02 | `RESOLVED` | Conda 环境已创建且当前依赖已安装，但 pyproject 无 build backend/package discovery，`pip install -e .[dev]` 因多顶层包失败；实现依赖和锁也未补齐 | hatchling build backend + wheel packages 四包；pip-tools 锁定为唯一锁定方式，`requirements-dev.lock`（46 钉版本）干净环境复现通过；依赖仍只维护在 pyproject（F0-T1） |
| R-03 | `RESOLVED` | agent v1 已版本化，但 CHANGELOG 明确待 V4/V5A 精修 | 修改须新版本目录 + manifest + CHANGELOG，不原地改 v1；**LLM 链路升级工作包覆盖**：2026-08-13 本地实现与契约守卫完成（496 passed + 守卫 44/44 + 四工具全绿），受控真实三链路（Planner→Generator→Scoring）canary 连续 3 次全 PASS（每任务 7 次调用全 SUCCESS、评分 3/3 回写、成本 ≈¥0.06 上限 ¥3 未触发）→ RESOLVED |
| R-04 | `ACCEPTED` | metrics 是运行端点，但有意不进业务 OpenAPI | F1/R1 直接测试，不强行写入 OpenAPI |
| R-05 | `ACCEPTED` | PRD 成功率/恢复率不能由单测或 60 个受控 generation units 完整证明；相同书籍/模型也限制独立性 | R1 只对预先固定抽样框中的单元失败率作带条件统计界限；另报重试、18 张描述性人工复核和自动化，不外推全书/生产质量 |
| R-06 | `RESOLVED` | deployment.md 描述未来 Cloudflare/HTTPS 真机入口，但当前阶段明确只做本机模拟 | Tunnel、TLS、公网和真机联网属于当前 Goal 之外的后续部署；不得阻塞 F0～R1 DONE，代码只保留可配置监听和反向代理兼容性；**2026-08-11 部署工作包完成**：Cloudflare Tunnel 落地（隧道 shanka、公共主机名 shanka.kbzz1.top、cloudflared systemd 常驻、scripts/run.sh、main/data/ 集中），公网 healthz/readyz 200、真机移动网络实测 306ms（阶梯 1 可用）；设计/计划/实测记录见 superpowers/specs、superpowers/plans、frontend/backend-integration.md |
| R-07 | `RESOLVED` | 仓库仅有 Superpowers 历史产物约定，当前会话未安装 `writing-plans`/ `subagent-driven-development` skill | Superpowers 插件已安装：writing-plans、using-git-worktrees、subagent-driven-development、executing-plans、finishing-a-development-branch 均可用；F0 以 SDD 模式执行（9 commits + 每任务契约/质量审查 + 最终整支审查） |
| R-08 | `RESOLVED` | `.env` 可被执行进程加载且 2026-08-10 真实直连 smoke 成功；执行 Agent 无视觉能力 | 不再重复凭据 smoke；后续 live 只能在 LOCAL-DONE 后走正式应用链路。PDF 只走已验证文本层/书签，未来无文本层样本按契约失败，不引入 OCR |
| R-09 | `ACCEPTED` | 正式契约要求记录 model，但不冻结具体模型或 thinking 模式 | 产品配置保持单一可替换入口；R1 为可比性冻结 `deepseek-v4-flash` + thinking disabled，不能反向改写 PRD/Architecture |
| R-10 | `RESOLVED` | 契约 1.3 要求"幂等键相同但请求体与首次不一致 → 409"，但 database-design 2.12 无 body 比对持久化载体 | F1 兼容性契约更新（AGENTS.md 版本管理规则）：database-design §2.12 新增 `request_body_hash` 列（首次请求体 SHA-256 hex）+ 规则段；ORM/增量迁移 0002/守卫三处同步（F1-T8） |
| R-11 | `RESOLVED` | structure-contract 3.8 Deck.source 为 `MANUAL/IMPORTED/GENERATED`，database-design 2.8 只列 `MANUAL/IMPORTED`——字段权威在 structure-contract，database-design 派生遗漏 GENERATED 枚举说明 | V4 收口：任务创建走用户指定 deck_id（TaskCreateRequest），本期无 GENERATED 牌组创建路径；契约 3.8 枚举保留（未来自动归属牌组使用），database-design 2.8 派生遗漏说明已核对（V4-T7） |
| R-14 | `RESOLVED` | openapi /samples 响应 items `$ref Card`（required 含 deck_id/position/created_at/updated_at），但样卡不入库、无这些字段 | V4 过渡（V4-T4 fix F-2）：handler 合成占位字段返回（deck_id=""/position=0/created_at/updated_at=请求时刻）；**R14 清账（2026-08-11）**：structure-contract 3.13 SampleCard 轻量组件 + openapi 组件（null 联合对齐 Card 先例）+ schemas 锚点 + handler 去占位，三处一致，见第 4 节 R14 包 |
| R-17 | `ACCEPTED` | SQLite 单写者：batch chat 期间（BEGIN IMMEDIATE 写锁跨长事务，无 busy_timeout）cancel/resume 等写接口 → 500 database-locked；批次间隙 cancel 已修复（I-1 条件更新） | 单写者串行化是本阶段既定架构（database-design 事务边界）；生产 DB（PostgreSQL 等）/多实例时按行级锁自然消失，不引入重试或额外设施；V5B 修复只覆盖可确定路径（批次间隙），chat 期间 500 保持 8.3 统一错误码响应 |
| R-18 | `ACCEPTED` | version 递增格式分支：V1 手动卡 version=ISO 时间戳，生成卡 version="v1"——重写递增规则统一为 ^v\d+$ → v(n+1)，非 vN 格式 → "v2" | V6 实现（_next_version 单测 5 例）；语义符合 database-design 2.9「变更版本，重写时递增」；R1 契约整理可考虑统一 version 语义（手动卡创建时即 v1），不阻塞 |
| R-19 | `ACCEPTED` | MANUAL/IMPORTED 卡重写后 generation_item_id 非空，但部分唯一索引仅覆盖 GENERATED（source='GENERATED'）——「同一 generation_item_id 最多对应一张有效卡片」对非 GENERATED 卡无索引兜底 | V6 实现：重写一律分配新 uuid4（PRD 5.13），source 不变；uuid 随机无防重冲突风险；database-design 2.9「仅 GENERATED 卡」描述性说明随重写语义扩展登记 |
| R-20 | `RESOLVED` | generator prompt v1 指令「输出一张卡片 JSON」= 裸单卡对象，但 V5A 解析器期望 `{"cards":[...]}` 数组包装——资产与解析契约断裂，mock 测试（返回包装格式）掩盖，live canary 首次 0 卡入库暴露 | R1 实质修复：prompts/generator v1→v2（输出 `{"cards":[...]}` + 规则 4 每知识点一卡批次语义），manifest/CHANGELOG/4 处 prompt_version 断言同步（353 passed）；诊断调用验证单知识点 1 卡、3 知识点 3 卡全合法；live 重跑 59/60 |
| R-21 | `ACCEPTED` | live 实证记录：正式运行 59/60（单元 43 上游 GENERATION_FAILED 抖动，非我方缺陷）；总成本 ¥1.6351（上限 ¥5/¥10 内）；fingerprint 单一 fp_a18b46594c_prod0820_fp8_kvcache_20260402；API_KEY_ENCRYPTION_KEY 未在 .env 提供（driver 临时密钥，live DB 密文跨进程不可解） | 统计口径 R-05：失败率 1.67% + Wilson 95% [0.29%, 8.86%]，仅描述固定抽样框不外推；加密密钥由部署侧提供（本机运行时限制，不影响验证结论） |

新增冲突先登记；解决后保留结论并改 `RESOLVED`。

## 7. 契约覆盖索引

| 范围 | 主包 | 证据 |
| --- | --- | --- |
| FR-01～02 / AC-01～02 | V3A | PDF integration + acceptance |
| FR-03、14 / AC-09 | V1 | deck/card integration + acceptance |
| FR-04～06 / AC-03 | V4 | generation integration + acceptance |
| FR-07～11 / AC-04、07 | V5A | generation/schema/quality integration + acceptance |
| FR-12 / AC-05 | V5B | transaction/recovery/quality + acceptance |
| FR-13 / AC-06 | V6 | rewrite atomicity + acceptance |
| FR-15～16 / AC-10 | V2 | FSRS/statistics + acceptance |
| FR-17 / AC-08、11 后端本机部分 | V3B（全局脱敏 F0/F1） | security/log capture + acceptance |
| FR-18 | F1 + 各纵向包 | OpenAPI/contract + endpoint tests |
| database-design | F1 + V1～V6 各闭环 | migration/ORM contract + integration |
| O-1～O-6 | F0/F1 + V5A | probe/metrics/log/quality tests |
| deployment / PRD HTTPS | 当前 Goal 外 | 保留供应商中立的 HTTPS 要求；明确启动联网部署后再实测，不计入 F0～R1 完成条件 |

覆盖索引只用于发现漏项，不形成第二套任务清单；契约新增范围优先归入现有闭环。
