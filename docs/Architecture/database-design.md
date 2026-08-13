# 数据库表设计 v2.1

持久化映射,字段定义源自 [structure-contract.md](structure-contract.md) 第 3 章资源模型;ORM 实现(`main/infra/db/`)必须与本设计一致。

## 0. 约定

- **数据库:SQLite**(MVP 选型,简单零运维;WAL 模式支持并发读 + 单写)。
- ORM 使用 SQLAlchemy(未来迁 PostgreSQL 时仅换 dialect,表语义与契约不变)。
- 类型映射:UUID → `TEXT`;时间 → `TEXT`(ISO 8601 UTC,与契约 1.2 一致,字符串比较即时间排序);JSON → `TEXT`;布尔 → `INTEGER(0/1)`;小数 → `REAL`;枚举 → `TEXT`。
- **时间格式唯一规范**:`YYYY-MM-DDTHH:MM:SS.sssZ`(UTC、零填充、恒 3 位毫秒),由统一序列化函数生成;禁止 `isoformat()` 默认输出(微秒省略、`+00:00` 偏移等变体)——混合格式会破坏 `due <= now` 范围比较与排序(审核修复)。
- **连接配置**(审核修复):`PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;`(SQLite 默认关闭外键)在 SQLAlchemy engine 级 connect 事件统一配置,覆盖池化连接、后台任务与迁移脚本;写事务用 `BEGIN IMMEDIATE`(engine `isolation_level='IMMEDIATE'`,进入即拿写锁,避免并发写直接 `SQLITE_BUSY`)。
- JSON 字段(`cursor`、`generated_item_ids`、`response_body` 等)统一经序列化函数写入,保证合法 JSON,禁止手工拼接。
- `device_id` 是数据主体隔离键:所有业务表按 `device_id` 的查询必须走索引。
- 幂等去重统一由 `idempotency_keys` 表承担(见 2.12),业务表不额外维护。

## 1. 实体关系概览

```text
devices 1──N pdf_files 1──N chapters
                    └──N text_chunks
devices 1──N decks 1──N cards 1──1 review_states
devices 1──N tasks 1──N batches ──N knowledge_points
devices 1──N review_events ──N cards
devices 1──1 api_keys
devices 1──N idempotency_keys
devices 1──N llm_call_attempts
```

## 2. 表定义

> 类型均按 0 节映射规则;时间列默认 `TEXT NOT NULL`(ISO 8601 UTC);枚举列存储枚举字符串。

### 2.1 devices

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| device_id | TEXT | PK | 匿名设备 ID(X-Device-ID) |
| first_seen_ip | TEXT | NULL | 首次注册 IP(风控信号,契约 1.1) |
| user_agent | TEXT | NULL | 首次 UA(风控信号) |
| last_active_at | TEXT | NULL | 最近活跃时间(风控信号) |
| created_at | TEXT | NOT NULL | |

注:`weekly_goal` 不落库 —— 由客户端本地保存、看板请求时上报(契约 3.12 / 6.8,审核修复)。

### 2.2 api_keys

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| device_id | TEXT | PK, FK → devices ON DELETE CASCADE | 一设备一 Key |
| encrypted_key | TEXT | NOT NULL | 加密存储(决策 D-03),仅 `infra/llm/` 使用;算法 AES-256-GCM,随机 IV 随密文保存,解密密钥来自环境变量,不随数据库备份导出(审核修复) |
| status | TEXT | NOT NULL | `AVAILABLE / INVALID / INSUFFICIENT_BALANCE / UNKNOWN` |
| masked_key | TEXT | NOT NULL | 脱敏标识,如 `sk-****abcd` |
| updated_at | TEXT | NOT NULL | |

### 2.3 pdf_files

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| file_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | |
| filename | TEXT | NOT NULL | |
| storage_key | TEXT | NOT NULL | 随机 UUID 存储路径,禁止含用户输入(filename 等);删除元数据时同步清理文件(契约 1.7,审核修复) |
| size_bytes | INTEGER | NOT NULL | |
| status | TEXT | NOT NULL | `PENDING / PARSING / PARSED / FAILED` |
| error_code | TEXT | NULL | `PDF_PARSE_FAILED / PDF_TOC_MISSING` |
| created_at | TEXT | NOT NULL | |

索引:`(device_id, created_at DESC)`(最近使用列表)。

### 2.4 chapters

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| chapter_id | TEXT | PK | |
| file_id | TEXT | NOT NULL, FK → pdf_files ON DELETE CASCADE | |
| name | TEXT | NOT NULL | 用户可修改 |
| start_page | INTEGER | NOT NULL | 用户可修改 |
| end_page | INTEGER | NOT NULL | 用户可修改 |

索引:`(file_id)`。

### 2.5 tasks

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| task_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | |
| file_id | TEXT | NULL, FK → pdf_files ON DELETE SET NULL | 删除 PDF 后任务保留,file_id 置空 |
| deck_id | TEXT | NULL, FK → decks ON DELETE SET NULL | 目标牌组;删除牌组后置空,任务保留(审核修复) |
| status | TEXT | NOT NULL | `PENDING / RUNNING / PAUSED / COMPLETED / FAILED / CANCELLED` |
| stage | TEXT | NULL | `PLANNING / GENERATING / SCORING` |
| selected_chapters | TEXT | NOT NULL | 章节快照(JSON),与源 chapter 解耦;**两阶段语义**:POST /tasks 写入创建快照(保留用户请求与 PENDING 视图);首次 PLANNING 抢占(CAS1)同一短事务内按快照 chapter_id 重读章节最新 name/start_page/end_page 覆盖并冻结为规划快照 |
| generation_config | TEXT | NOT NULL | 数量倾向/难度比例/自定义要求(JSON) |
| cursor | TEXT | NULL | `{ "completed_batch_count": int }`(JSON);游标为唯一源,与 `completed_batch_count` 列同事务原子写入 |
| generated_card_count | INTEGER | NOT NULL DEFAULT 0 | 已入库卡片数 |
| total_batch_count | INTEGER | NULL | 规划完成后写入 |
| completed_batch_count | INTEGER | NULL | |
| completion_reason | TEXT | NULL | 空单元三分支:`NO_GENERATION_UNITS`(全组成功但 0 个合法单元,COMPLETED) |
| skipped_planning_group_count | INTEGER | NOT NULL DEFAULT 0 | 部分规划组失败被跳过的组数 |
| resumable | INTEGER | NOT NULL DEFAULT 0 | 0/1 |
| failure_stage | TEXT | NULL | `PLANNING / GENERATING / SCORING / WRITE_BACK` |
| error_code | TEXT | NULL | |
| created_at / started_at / ended_at / updated_at | TEXT | 按需 | |

索引:`(device_id, created_at DESC)`;`(task_id, device_id)`。

### 2.6 knowledge_points

表名保持 `knowledge_points`(生成单元兼容壳,契约 3.6);"知识点"语义已升级为**生成单元**——最小规划单元 = 一个锚定卡片类型与目标难度的生成任务。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| knowledge_point_id | TEXT | PK | 单元 ID(服务端生成) |
| task_id | TEXT | NOT NULL, FK → tasks ON DELETE CASCADE | |
| chapter_id | TEXT | NULL | 章节快照引用;章节删除后置空,名称经 `tasks.selected_chapters` 快照还原(契约 3.6) |
| source_chunk_ids | TEXT | NULL | 来源页文本标识列表(`text_chunks.chunk_id`,TEXT JSON,一页一个);运行时取原文以本列为权威;旧数据无值,新数据代码保证必填 |
| source_chunk_id | TEXT | NOT NULL | 兼容投影列 = `source_chunk_ids[0]`(新单元写入;旧数据继续按此列读取) |
| topic | TEXT | NOT NULL | 学习目标(Planner 输出,语义复用;不再"第X章-知识点N"占位) |
| target_difficulty | TEXT | NULL | 规划锚定:`BASIC / UNDERSTANDING / APPLICATION`;旧数据无值,新数据保证必填 |
| card_type | TEXT | NULL | 规划锚定:`QUESTION / TRUE_FALSE`;旧数据无值,新数据保证必填 |
| priority | INTEGER | NOT NULL | 全局顺序(服务端按章序、组序、数组顺序合并分配) |
| status | TEXT | NOT NULL | `PENDING / PROCESSED / SKIPPED` |

索引:`(task_id)`。

### 2.7 batches

**批 = 生成单元**:每单元 1 批、1 次生成调用(1 单元 1 卡);删除 offset 反推知识点的旧逻辑。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| batch_id | TEXT | PK | |
| task_id | TEXT | NOT NULL, FK → tasks ON DELETE CASCADE | |
| generation_unit_id | TEXT | NULL, FK → knowledge_points ON DELETE SET NULL | 生成单元外键;新数据保证必填;迁移列为 NULL 仅兼容旧批次 |
| batch_index | INTEGER | NOT NULL | 批次序号(游标)= 单元序号(1..N) |
| status | TEXT | NOT NULL | `PENDING / PROCESSING / SUCCEEDED / FAILED / SKIPPED` |
| generated_item_ids | TEXT | NOT NULL DEFAULT '[]' | 本批 `generation_item_id` 列表(JSON;每单元 1 卡,成功时为单值) |
| retry_count | INTEGER | NOT NULL DEFAULT 0 | 重试计数(生成阶段兼容投影;尝试数与重试预算以 `llm_call_attempts` 为权威) |
| coverage_rate / duplicate_rate | REAL | NULL | 质量观测(FR-10);`coverage_rate` = 该单元是否产出合法卡(0/1,不再恒 1.0),SKIPPED 批次 = 0 |
| difficulty_distribution / chapter_distribution / card_type_distribution | TEXT | NULL | JSON,同上;批=单元后为单值分布 |
| difficulty_deviation | REAL | NULL | 难度偏差(契约 3.7,审核修复) |
| cache_hit_tokens / cache_miss_tokens / output_tokens | INTEGER | NULL | Prompt Cache(FR-11);生成阶段兼容投影,全阶段 token 权威在 `llm_call_attempts` |
| request_id | TEXT | NULL | 模型请求标识(请求层观测,PRD 6.2) |
| model / prompt_version / schema_version / rubric_version | TEXT | NULL | 版本观测(FR-11);由同一次调用结果同步写入,各调用实际 asset name+version 在 `llm_call_attempts` 逐调用记录 |
| duration_ms | INTEGER | NULL | 请求耗时 |
| http_status | INTEGER | NULL | 上游 HTTP 状态 |
| created_at / ended_at | TEXT | 按需 | |

唯一约束:`UNIQUE (task_id, batch_index)`(游标完整性,断点续传依据);`UNIQUE (task_id, generation_unit_id)`(新数据必填;SQLite 下 NULL 不参与冲突,兼容旧批次)。

### 2.8 decks

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| deck_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | |
| name | TEXT | NOT NULL | |
| source | TEXT | NOT NULL | `MANUAL / IMPORTED` |
| version | TEXT | NOT NULL | 变更版本,客户端缓存刷新用 |
| created_at / updated_at | TEXT | NOT NULL | |

索引:`(device_id, updated_at DESC)`。

### 2.9 cards

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| card_id | TEXT | PK | |
| deck_id | TEXT | NOT NULL, FK → decks ON DELETE CASCADE | |
| device_id | TEXT | NOT NULL | 冗余列,服务端维护与 deck 一致 |
| source | TEXT | NOT NULL | `GENERATED / MANUAL / IMPORTED` |
| position | INTEGER | NOT NULL | 牌组内排序位置,追加时分配;`UNIQUE (deck_id, position)`(审核修复) |
| front / back | TEXT | NOT NULL | 通用渲染字段 |
| code | TEXT | NULL | 卡片编号 |
| card_type | TEXT | NOT NULL | `QUESTION / TRUE_FALSE` |
| question / answer | TEXT | NULL | 仅 QUESTION 卡(决策 D-01) |
| statement / explanation | TEXT | NULL | 仅 TRUE_FALSE 卡 |
| answer_boolean | INTEGER | NULL | 仅 TRUE_FALSE 卡(0/1) |
| generation_item_id | TEXT | NULL | 仅 GENERATED 卡 |
| target_difficulty | TEXT | NULL | 仅 GENERATED 卡;`BASIC / UNDERSTANDING / APPLICATION` |
| knowledge_point_ids | TEXT | NULL | 仅 GENERATED 卡;JSON 数组,综合应用卡可多个(审核修复) |
| evidence_score / correctness_score / difficulty_score / learning_value_score | INTEGER | NULL | Rubric 各维度 0~3,仅 GENERATED 卡(审核修复) |
| rubric_total_score | INTEGER | NULL | Rubric 总分 0~12,仅 GENERATED 卡 |
| version | TEXT | NOT NULL | 变更版本,重写时递增(审核修复) |
| created_at / updated_at | TEXT | NOT NULL | |

约束与索引:

- **部分唯一索引**(SQLite 支持):`CREATE UNIQUE INDEX idx_cards_gen_item ON cards(generation_item_id) WHERE source = 'GENERATED' AND generation_item_id IS NOT NULL`(AC-05 重复入库率为 0)。
- 唯一索引:`UNIQUE (deck_id, position)`(追加 position 分配的并发保护)。
- 索引:`(device_id, deck_id)`。

### 2.10 review_states

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| review_state_id | TEXT | PK | |
| card_id | TEXT | UNIQUE, FK → cards ON DELETE CASCADE | 与卡片一对一 |
| state | TEXT | NOT NULL | `NEW / LEARNING / REVIEW / RELEARNING`(FSRS) |
| stability | REAL | NOT NULL | FSRS 稳定性(天);`CHECK (stability >= 0)`(审核修复) |
| difficulty | REAL | NOT NULL | FSRS 难度(1~10,py-fsrs 口径);`CHECK (difficulty >= 1 AND difficulty <= 10)` |
| due | TEXT | NOT NULL | 下次到期(服务端时间,统一时间格式) |
| last_review | TEXT | NULL | |
| reps | INTEGER | NOT NULL DEFAULT 0 | 复习次数 |
| lapses | INTEGER | NOT NULL DEFAULT 0 | 遗忘次数 |
| last_rating | TEXT | NULL | `AGAIN / HARD / GOOD / EASY` |
| updated_at | TEXT | NOT NULL | |

索引:`(due)`(`card_id` 由 UNIQUE 自带索引,无需独立索引,审核修复)。

### 2.11 review_events

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| review_event_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | |
| card_id | TEXT | NOT NULL, FK → cards ON DELETE CASCADE | |
| client_event_id | TEXT | NOT NULL | 客户端生成 |
| rating | TEXT | NOT NULL | `AGAIN / HARD / GOOD / EASY` |
| reviewed_at | TEXT | NOT NULL | 服务端时间 |
| device_timezone | TEXT | NOT NULL | IANA 时区,仅看板分桶 |
| created_at | TEXT | NOT NULL | |

约束与索引:

- 唯一约束:`UNIQUE (device_id, client_event_id)`(离线重试去重,AC-10)。
- 索引:`(device_id, reviewed_at DESC)`(看板聚合);`(card_id)`。

不可变记录:不提供 UPDATE / DELETE。

### 2.12 idempotency_keys

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| idempotency_key | TEXT | 复合主键 | 请求头 `Idempotency-Key` |
| device_id | TEXT | 复合主键 | |
| path | TEXT | 复合主键 | 接口路径**含具体资源 ID**(`/v1/pdfs/<file_id>`),避免同一设备对不同资源复用同键被静默吞掉(审核修复) |
| response_status | INTEGER | NOT NULL | 首次成功响应状态码 |
| response_body | TEXT | NOT NULL | 首次成功响应体快照(JSON,重放用) |
| request_body_hash | TEXT | NOT NULL | 首次请求体 SHA-256 摘要(hex)；幂等键相同但摘要与首次不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3 比对的持久化载体,审核补全) |
| created_at | TEXT | NOT NULL | |

主键:`PRIMARY KEY (device_id, path, idempotency_key)`(审核修复)。

规则:

- 仅记录成功(2xx)响应;失败的重复请求直接重试执行。
- 重复请求:返回快照 `response_status + response_body`,不执行任何副作用;**幂等记录 INSERT 与业务副作用必须在同一事务内**(响应丢失后同键重试不双写,AC-05/AC-10)。
- 请求体一致性:重复请求携带相同 `Idempotency-Key` 时,比对 `request_body_hash` 与首次记录;不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3)。
- 保留策略:TTL 90 天(幂等窗口只需覆盖客户端重试周期),清理任务定期删除过期记录(审核修复)。

### 2.13 text_chunks

按文件页码持久化文本(LLM 链路升级工作包新增):scanner PARSED 时完整解析每页文本,**一页一行、与章节解耦**——不保存 `chapter_id`,不在扫描阶段按章节切块;章节名称/页码可在 PARSED 后修改,页文本不随章节编辑、删除而重建。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| chunk_id | TEXT | PK | 服务端按 `(file_id, page_number, content_sha256)` 确定性生成;同一 PDF 内容的页标识稳定 |
| file_id | TEXT | NOT NULL, FK → pdf_files ON DELETE CASCADE | 删除 PDF 时按 file_id 级联清理全文页数据 |
| page_number | INTEGER | NOT NULL | 页码(一页一行) |
| char_count | INTEGER | NOT NULL | 页字符数(规划分组/配额依据) |
| content_sha256 | TEXT | NOT NULL | 页文本摘要(输入指纹/漂移检测依据) |
| content | TEXT | NOT NULL | 完整页文本(功能数据;完整 PDF 文本/完整 Prompt/原文样例仍禁止写日志、审计与调用账本) |
| created_at | TEXT | NOT NULL | |

唯一约束:`UNIQUE (file_id, page_number)`。索引:`(file_id, page_number)`。
重解析幂等:先清理该 file_id 的既有页文本再重建;章节范围内所有页均无有效文本时该章不发 Planner 请求,作为成功空结果处理。

### 2.14 llm_call_attempts

LLM 调用账本(LLM 链路升级工作包新增):**重试预算、调用上限与全阶段 token 的权威**。任何外部 chat 调用必须先有已提交的 STARTED 占位行(调用前持久化),Task 上不设冗余计数列。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| call_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | 数据归属 |
| scope_type / scope_id | TEXT | NOT NULL | `TASK` / `CARD`;任务链路 scope_id=task_id,单卡重写 scope_id=card_id |
| task_id | TEXT | NULL, FK → tasks ON DELETE CASCADE | 可空;手动/导入卡重写没有生成任务 |
| stage | TEXT | NOT NULL | `PLANNING / GENERATING / SCORING / REWRITE` |
| operation_key | TEXT | NOT NULL | 规划含 chapter/group/input fingerprint;生成含 batch_id;评分含确定性 group key;重写含 card_id/card_version/Idempotency-Key hash |
| attempt_no | INTEGER | NOT NULL | 同一操作的第几次实际尝试 |
| input_fingerprint | TEXT | NOT NULL | 输入身份(不保存完整 Prompt/原文) |
| model | TEXT | NOT NULL | 实际模型值 |
| prompt_name / prompt_version | TEXT | NOT NULL | 本调用实际使用的 prompt 资产名/版本 |
| schema_name / schema_version | TEXT | NULL | 本调用实际使用的 output schema;不适用则 NULL |
| rubric_version | TEXT | NULL | 本调用实际使用的 rubric 版本;不适用则 NULL |
| cache_hit / cache_miss / output_tokens | INTEGER | NULL | usage 原样 |
| http_status | INTEGER | NULL | 上游 HTTP 状态 |
| duration_ms | INTEGER | NULL | 请求耗时 |
| status | TEXT | NOT NULL DEFAULT 'STARTED' | `STARTED / SUCCESS / FAILED / UNKNOWN` |
| error_code | TEXT | NULL | 失败类别 |
| normalized_result | TEXT | NULL | 仅 PLANNING 成功时保存通过校验的规范化 units JSON;不保存原文、完整 Prompt 或原始模型响应 |
| created_at / finished_at | TEXT | 按需 | 调用占位与结束时间 |

唯一约束:`UNIQUE (scope_type, scope_id, stage, operation_key, attempt_no)`。索引:`(device_id, created_at)`、`(task_id, stage, operation_key)`。
规则:

- 重试判定:该 operation_key 的 STARTED/SUCCESS/FAILED/UNKNOWN 尝试总数达到预算 → 不再发请求;孤儿 STARTED 转 UNKNOWN 并计数,不能仅统计 FAILED。
- 同一 `operation_key + input_fingerprint` 最多一个 SUCCESS;领域写入与调用终态在同一事务提交,禁止账本已 SUCCESS 但业务结果未落库。
- Rewrite 的 operation_key 必须区分同一卡片的多次用户请求(不得用 `rewrite:{card_id}` 让历史失败影响后续合法重写)。
- Scoring 上限:账本 stage=SCORING 的全部尝试数 ≤ `max_scoring_calls_per_task`(抽样预保证 + 调用前账本条件校验)。
- 成本口径:账本是 Planner/Generator/Scoring/Rewrite 总 token 的唯一来源;Batch token 仅是 GENERATING 的兼容投影,不得再次相加双计。

## 3. 级联与并发

| 删除对象 | 级联效果 |
| --- | --- |
| decks | cards → review_states、review_events 全部 CASCADE;tasks.deck_id SET NULL(存在非终态任务引用时删除被 `409 TASK_IN_PROGRESS` 拒绝,契约 6.5) |
| pdf_files | chapters CASCADE;tasks.file_id SET NULL(存在非终态任务引用时删除被 `409 TASK_IN_PROGRESS` 拒绝,契约 6.1) |
| tasks | knowledge_points、batches CASCADE(本期无任务删除接口,预留) |
| cards | review_states、review_events CASCADE |

一致性规则:

- **连接配置**:每个连接开启 `PRAGMA foreign_keys=ON`(否则级联不生效);`PRAGMA journal_mode=WAL`(并发读 + 单写,满足 MVP 轮询 + 写并发场景)。两者在 SQLAlchemy engine 级 connect 事件统一配置,覆盖池化连接、后台任务与迁移脚本。
- **写事务**:SQLite 单写者;写操作使用 `BEGIN IMMEDIATE` 事务(engine `isolation_level='IMMEDIATE'`,进入即拿写锁)。事务边界:`create deck + cards`、`submit review event + update review_state`、`batch 完成 + 更新 task 游标`、**幂等记录 INSERT + 业务副作用** 各自在同一事务内;统计聚合可异步,但成功响应后最终一致(PRD 6.6)。
- **resume 并发防护**:`resume` 在 `BEGIN IMMEDIATE` 事务内先校验 `status == 'PAUSED' AND resumable = 1` 再置 `RUNNING`,状态校验失败返回 `TASK_STATE_CONFLICT`;孤儿 `RUNNING`(心跳 `updated_at` 超过 30 分钟)允许抢占:条件更新 `status='RUNNING' AND updated_at < now-30min`(契约 4.1)。
- 新建卡片时同事务插入初始 `review_states`(state=NEW,初始排程参数,审核修复)。
- 单卡重写(FR-13,决策 C-05):原地更新 `cards` 行(新内容、新 `generation_item_id`,`position` 不变,`updated_at` / `version` 递增),`review_states` 重置为新卡初始状态;旧 `generation_item_id` 随列覆盖自然作废。
- `cards.device_id` 由服务端写入,保证与 `decks.device_id` 一致;所有资源归属校验按 `device_id` 过滤(无 `device_id` 列的表如 chapters、knowledge_points、batches、review_states 经 FK join 到所属设备)。

## 4. 看板聚合实现说明

MVP 直接基于 `review_events` 聚合(索引 `(device_id, reviewed_at DESC)` 已覆盖),不建物化聚合表。统计口径见 PRD 5.16 与结构契约 3.12。单设备数据量(千级卡片、万级事件)下 SQLite 聚合毫秒级完成,若后续数据量增长再引入异步聚合(PRD 6.6 允许)。

## 5. 迁移路径(未来,不在本期实现)

若上线后需要多实例部署或跨设备同步,迁移 PostgreSQL:

- ORM 层已用 SQLAlchemy,换 dialect 即可;表结构与契约语义不变。
- 需调整:时间列 TEXT → `timestamptz`、JSON 列 TEXT → `jsonb`、`resume` 并发防护改用 `SELECT ... FOR UPDATE`、迁移工具生成 DDL。
- 接口契约(`openapi.yaml`)不受影响。

## 6. 与结构契约的映射

| 表 | 契约资源 |
| --- | --- |
| devices / api_keys | 3.1 ApiKey |
| pdf_files / chapters | 3.2 PdfFile / 3.3 Chapter |
| tasks / generation_config | 3.4 Task / 3.5 GenerationConfig |
| knowledge_points | 3.6 KnowledgePoint(生成单元) |
| batches | 3.7 Batch |
| decks | 3.8 Deck |
| cards | 3.9 Card |
| review_states | 3.10 ReviewState |
| review_events | 3.11 ReviewEvent |
| (聚合计算) | 3.12 StatsDashboard |
| idempotency_keys | 总则 1.3 幂等约定 |
| text_chunks | 3.6 KnowledgePoint 来源页底座(来源分片标识) |
| llm_call_attempts | 3.7 Batch / 8.5 评估骨架(调用账本) |

## 7. 演进路径

### 7.1 账号体系（未来）

- 新增 `users` 表;`devices` 增加可空外键列 `user_id`(先 NULL 后回填)。**不重构 devices 主键**,匿名设备 ID 体系维持为兼容层。
- 数据迁移:按绑定关系批量回填 `user_id` 后加 NOT NULL;业务表隔离键仍为 `device_id`。

### 7.2 新卡类型（未来）

- 沿用 D-01 模式:专用列 + `front`/`back` 通用渲染。
- 类型数可控(≤5)时继续用专用列;字段高度异构或继续膨胀时,评估 JSON 扩展列方案。
- 所有结构变更走迁移工具。

### 7.3 迁移工具选型

- 选型:**Alembic**(SQLAlchemy 官方迁移工具);P0-2 引入并生成首个迁移。
- 迁移纪律:与 ORM 模型同 PR 提交;破坏性变更需同步更新 database-design 与契约。
