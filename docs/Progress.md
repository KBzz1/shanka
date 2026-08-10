# 后端实现执行地图（v2.1）

本文是后续 Goal 的唯一进度与状态地图，不建立并列计划或看板。需求权威为 [PRD v2.1](PRD/V2.1/prd_v2_1.md)，实现以 [Architecture](Architecture/AGENTS.md) 的防漂移规则为准。Superpowers plan 只细化当前一个工作包的实施步骤，不定义项目状态，也不替代正式契约。

最后事实审计：2026-08-10。

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
| 可运行后端 | `DONE`（F0 基线） | create_app 装配（settings/engine/storage state + lifespan dispose）+ healthz/readyz 探针 + 统一错误 handler + LocalStorage 就绪探测；业务路由随 V1+ 纵向包逐步接入 |
| 自动化验证 | `DONE`（F0 首批） | 34 passed：unit（Settings/errors/format_utc/clock）+ integration（engine WAL/外键/空库、probes 200/503、error handler 1.4 形状）+ contract（四类守卫含负例）；四工具命令全绿 |
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

**`TODO`｜依赖：F0｜覆盖：structure-contract 1.1～1.7、database-design 0～3、O-1/O-3**

实现 12 张表、约束、索引、外键、WAL、Alembic 初始迁移；统一 X-Device-ID、Idempotency-Key 指纹/首次响应重放、错误包装、request_id、限流；提供请求级 session、幂等请求指纹、并发占位、唯一约束和首次响应存储原语，跨设备统一 404；JSON 日志与 HTTP/限流指标；metrics 返回 Prometheus 文本。

验收：空库 upgrade/downgrade/upgrade；磁盘 SQLite 外键/WAL；幂等原语的并发占位与回滚、隔离、429 + Retry-After、探针豁免 integration 测试。业务副作用与首次响应的完整同事务验收在 V1 首个真实写接口完成。

### V1 — 牌组与卡片闭环

**`TODO`｜依赖：F1｜覆盖：FR-03/14/18、接口 6.5、AC-09**

实现牌组列表/创建/详情/删除、自由刷题列表、手动新增、原子批量导入；统一 Card/position/source；真实进度查询；删除保护、级联及历史任务 deck_id 置空。

验收：对应操作走真实 DB；追加不覆盖、导入全成或全败、逐张结果、稳定 position、重复删除和任务保护；同设备同 key 同请求单副作用并重放原响应，同 key 异请求冲突，失败时业务与幂等记录共同回滚，新 app/session 可重放，并发不双写；AC-09 通过。

### V2 — FSRS 复习与看板闭环

**`TODO`｜依赖：V1｜覆盖：FR-15/16、weekly_goal 边界、接口 5/6.6/6.8、AC-10**

以单一适配封装 py-fsrs 契约参数；实现到期队列、四档评级、client_event_id 去重、ReviewState 快照、牌组进度和 IANA 时区周看板。

验收：契约 5.1 确定性断言；事务回滚；重复/冲突事件；排序；周一分桶、DST/跨周、连续天数、首次/非首次、零分母 null、weekly_goal 缺省及 AC-10。

### V3A — PDF 生命周期闭环

**`TODO`｜依赖：F1｜可与 V1/V2/V3B 并行｜覆盖：FR-01/02/18、接口 6.1、AC-01/02 及 AC-08 后端存储边界**

实现 PDF 三重校验、大小/页数限制、受控存储、文本层/真实目录解析、轮询、章节 PATCH、最近列表、删除保护；不做 OCR 或目录兜底。解析由进程内、DB 驱动的可重启扫描器执行。验收书籍 `res/AI-Agents-in-Depth-zh-CN.pdf` 已有文本层和书签目录，执行 Agent 只需程序化解析文本/书签，不依赖视觉、页面截图或 OCR。

验收：有效/无目录/扫描件/损坏/伪 MIME/超限、路径穿越、隔离；以磁盘 DB 和文件存储启动新 app/worker 验证恢复；章节范围、删除清理及 AC-01/02 的本机后端行为通过。

### V3B — API Key 安全与 DeepSeek 适配边界

**`TODO`｜依赖：F1｜可与 V1/V2/V3A 并行｜覆盖：FR-17/18、接口 1.5/6.2、AC-08/11 的后端本机部分**

实现 Key 状态映射、AES-256-GCM 环境密钥加密与覆盖规则，仅 infra/llm 解密；完成正式 DeepSeek adapter。模型与 thinking 模式通过单一配置入口注入并记录实际值，不在领域/业务层硬编码；应用服务使用 deterministic fake，正式 adapter 使用 mock HTTP transport，不访问外网。

验收：AVAILABLE/INVALID/余额不足/上游不可用、旧有效 Key 保护；adapter 请求鉴权、thinking 开关、JSON 输出约束、超时、畸形响应、错误码和脱敏；数据库、响应、日志、异常、任务与分析数据均无明文。真实 Key/余额与客户端存储不在本机完成声明内；本次直连 smoke 不计为 adapter 验收。

### V4 — 样卡、任务与知识点规划

**`TODO`｜依赖：V1 + V3A + V3B｜覆盖：FR-04/05/06/18、接口 3.5/4.1/4.2/6.3/6.4、AC-03**

按 manifest 加载/校验资产；Prompt 稳定前缀与动态后缀分离；固定构成 3 张样卡且不入库；创建/查询/取消任务，持久化配置/章节，规划 KnowledgePoint，以 DB 条件更新抢占执行。

验收：样卡构成；无 Key/章节/牌组、非法比例；COMPACT ≤ BALANCED ≤ EXTENSIVE；自定义要求不继承；同 key 单任务；状态转移和 AC-03。

### V5A — 分批生成与质量观测闭环

**`TODO`｜依赖：V4｜覆盖：FR-07～11/18、接口 4/6.4/6.9/6.10/8、AC-04/07**

按知识点分批，每批最多 2 次重试；Schema 是唯一入库门槛，Rubric 只观测。合法卡、generation_item_id、知识点/批次状态、游标、计数原子推进；失败批次 SKIPPED 后继续。记录实际 model/system_fingerprint、prompt/cache-miss/cache-hit/output token、Prompt/Schema/Rubric 版本、Rubric 与质量；提供批次/质量聚合/指标/按生效日期配置的成本估算。

验收：低 Rubric 合法卡入库；非法输出三次后跳过；已完成批次和 generation_item_id 不重复；provider usage 原样字段与内部统一字段映射受测，价格调整不改历史 token 数据；版本/缓存/质量/成本可核验；AC-04/07。

### V5B — 任务恢复、取消与并发闭环

**`TODO`｜依赖：V5A｜覆盖：FR-12/18、任务状态机、AC-05**

在 V4 的唯一任务状态机上补 checkpoint/resume/cancel、RUNNING 心跳、30 分钟孤儿恢复和 DB 条件抢占；不建立第二套任务框架，不引入外部队列。

验收：以磁盘 DB/文件存储注入崩溃并创建新 app/session/worker 恢复；并发 worker/resume 单执行者；完成批次和 generation_item_id 不重复；取消保留已入库卡；AC-05 通过。

### V6 — 单卡重写闭环

**`TODO`｜依赖：V5B + V2｜覆盖：FR-13、接口 6.7/C-05、AC-06**

同 card_id 原地替换、position 不变、version 递增、新 generation_item_id、Rubric 记录，原子重置 ReviewState；失败不改变内容/排程；来源不出响应。

验收：成功、Schema/LLM 失败、并发、幂等重放/冲突、隔离和 AC-06。

### R1 — 本机契约回归、受控真实模型验证与交付

**`TODO`｜依赖：V1、V2、V3A、V3B、V4、V5A、V5B、V6｜覆盖：PRD 7～10 的后端可本机验证部分、AC-01～11**

只做跨闭环收敛，不在此补主体功能。先运行 contract/acceptance，通过 TestClient 或 localhost 核对路径、错误、脱敏、文件/DB 恢复、慢查询和 PRD 8 可本机采样指标，并清除 Mock/硬编码成功路径；全部本机门槛通过后才开放受控 DeepSeek live 验证。Cloudflare Tunnel、TLS 证书和 Android 真机联网仍不在本阶段执行。

验收：四个工具命令及 AC-01～11 中属于后端职责的本机行为全通过；干净本机环境完成锁定安装、启动、迁移和重启恢复；通过 TestClient/localhost 完成 PDF 制卡、牌组、复习、看板、Key、重写及 healthz/readyz/metrics 验证。生产 DeepSeek adapter 必须完成；应用编排使用可控 fake，adapter 使用 mock HTTP transport 验证请求、超时、解析、错误码和脱敏。

live 验证采用“有统计界限的分层独立生成单元”而非全书大跑：从本书第 1、2、6 章各确定 20 个分散文本块，共 60 个 generation unit，easy/medium/hard = 24/24/12；每个单元按正式链路独立执行，单元成功要求其最终响应 Schema 合法且入库、计数、幂等均正确，原始 attempt/retry 另行统计。第一个单元同时作为 canary；失败即停止，成功则继续同一次正式运行，不再增加单独 smoke。

正式样本默认只运行 1 次，只有发生实质修复才允许完整重跑 1 次；每次硬上限 ¥5，总上限 ¥10，达到上限立即停止并保留真实失败。验证配置冻结为 `deepseek-v4-flash`、thinking disabled、JSON output，并记录实际 model、system_fingerprint、token、版本和当日价格配置。若 60/60 generation units 成功，在预先固定样本且近似独立的条件下可报告单侧 95% 失败率上界约 4.9%；若有失败则报告原始比例与精确区间，不得外推为全书或生产质量。人工质量复核从产出卡中按章节和难度分层抽 18 张，只作描述性报告；Rubric 仍只观测、不作为入库门槛。

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
| R-03 | `OPEN` | agent v1 已版本化，但 CHANGELOG 明确待 V4/V5A 精修 | 修改须新版本目录 + manifest + CHANGELOG，不原地改 v1 |
| R-04 | `ACCEPTED` | metrics 是运行端点，但有意不进业务 OpenAPI | F1/R1 直接测试，不强行写入 OpenAPI |
| R-05 | `ACCEPTED` | PRD 成功率/恢复率不能由单测或 60 个受控 generation units 完整证明；相同书籍/模型也限制独立性 | R1 只对预先固定抽样框中的单元失败率作带条件统计界限；另报重试、18 张描述性人工复核和自动化，不外推全书/生产质量 |
| R-06 | `ACCEPTED` | deployment.md 描述未来 Cloudflare/HTTPS 真机入口，但当前阶段明确只做本机模拟 | Tunnel、TLS、公网和真机联网属于当前 Goal 之外的后续部署；不得阻塞 F0～R1 DONE，代码只保留可配置监听和反向代理兼容性 |
| R-07 | `RESOLVED` | 仓库仅有 Superpowers 历史产物约定，当前会话未安装 `writing-plans`/ `subagent-driven-development` skill | Superpowers 插件已安装：writing-plans、using-git-worktrees、subagent-driven-development、executing-plans、finishing-a-development-branch 均可用；F0 以 SDD 模式执行（9 commits + 每任务契约/质量审查 + 最终整支审查） |
| R-08 | `RESOLVED` | `.env` 可被执行进程加载且 2026-08-10 真实直连 smoke 成功；执行 Agent 无视觉能力 | 不再重复凭据 smoke；后续 live 只能在 LOCAL-DONE 后走正式应用链路。PDF 只走已验证文本层/书签，未来无文本层样本按契约失败，不引入 OCR |
| R-09 | `ACCEPTED` | 正式契约要求记录 model，但不冻结具体模型或 thinking 模式 | 产品配置保持单一可替换入口；R1 为可比性冻结 `deepseek-v4-flash` + thinking disabled，不能反向改写 PRD/Architecture |

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
