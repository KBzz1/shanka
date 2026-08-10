# 数据库表设计 v2.1

持久化映射,字段定义源自 [structure-contract.md](structure-contract.md) 第 3 章资源模型;ORM 实现(`main/infra/db/`)必须与本设计一致。

## 0. 约定

- **数据库:SQLite**(MVP 选型,简单零运维;WAL 模式支持并发读 + 单写)。
- ORM 使用 SQLAlchemy(未来迁 PostgreSQL 时仅换 dialect,表语义与契约不变)。
- 类型映射:UUID → `TEXT`;时间 → `TEXT`(ISO 8601 UTC,与契约 1.2 一致,字符串比较即时间排序);JSON → `TEXT`;布尔 → `INTEGER(0/1)`;小数 → `REAL`;枚举 → `TEXT`。
- 每个连接必须执行:`PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;`(SQLite 默认关闭外键)。
- `device_id` 是数据主体隔离键:所有业务表按 `device_id` 的查询必须走索引。
- 幂等去重统一由 `idempotency_keys` 表承担(见 2.12),业务表不额外维护。

## 1. 实体关系概览

```text
devices 1──N pdf_files 1──N chapters
devices 1──N decks 1──N cards 1──1 review_states
devices 1──N tasks 1──N batches
               │ └──N knowledge_points
devices 1──N review_events ──N cards
devices 1──1 api_keys
devices 1──N idempotency_keys
```

## 2. 表定义

> 类型均按 0 节映射规则;时间列默认 `TEXT NOT NULL`(ISO 8601 UTC);枚举列存储枚举字符串。

### 2.1 devices

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| device_id | TEXT | PK | 匿名设备 ID(X-Device-ID) |
| weekly_goal | INTEGER | NULL | 周目标;未配置为 NULL |
| created_at | TEXT | NOT NULL | |

### 2.2 api_keys

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| device_id | TEXT | PK, FK → devices ON DELETE CASCADE | 一设备一 Key |
| encrypted_key | TEXT | NOT NULL | 加密存储(决策 D-03),仅 `infra/llm/` 使用 |
| status | TEXT | NOT NULL | `AVAILABLE / INVALID / INSUFFICIENT_BALANCE / UNKNOWN` |
| masked_key | TEXT | NOT NULL | 脱敏标识,如 `sk-****abcd` |
| updated_at | TEXT | NOT NULL | |

### 2.3 pdf_files

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| file_id | TEXT | PK | |
| device_id | TEXT | NOT NULL, FK → devices | |
| filename | TEXT | NOT NULL | |
| storage_key | TEXT | NOT NULL | 文件系统存储路径,文件内容不入库 |
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
| deck_id | TEXT | NOT NULL, FK → decks | 目标牌组 |
| status | TEXT | NOT NULL | `PENDING / RUNNING / PAUSED / COMPLETED / FAILED / CANCELLED` |
| stage | TEXT | NULL | `PLANNING / GENERATING` |
| selected_chapters | TEXT | NOT NULL | 章节快照(JSON),与源 chapter 解耦 |
| generation_config | TEXT | NOT NULL | 数量倾向/难度比例/自定义要求(JSON) |
| cursor | TEXT | NULL | `{ "completed_batch_count": int }`(JSON) |
| generated_card_count | INTEGER | NOT NULL DEFAULT 0 | 已入库卡片数 |
| total_batch_count | INTEGER | NULL | 规划完成后写入 |
| completed_batch_count | INTEGER | NULL | |
| resumable | INTEGER | NOT NULL DEFAULT 0 | 0/1 |
| failure_stage | TEXT | NULL | `PLANNING / GENERATING / WRITE_BACK` |
| error_code | TEXT | NULL | |
| created_at / started_at / ended_at / updated_at | TEXT | 按需 | |

索引:`(device_id, created_at DESC)`;`(task_id, device_id)`。

### 2.6 knowledge_points

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| knowledge_point_id | TEXT | PK | |
| task_id | TEXT | NOT NULL, FK → tasks ON DELETE CASCADE | |
| chapter_id | TEXT | NULL | 章节快照引用;章节删除后保留 |
| source_chunk_id | TEXT | NOT NULL | 来源分片标识 |
| topic | TEXT | NOT NULL | |
| priority | INTEGER | NOT NULL | |
| status | TEXT | NOT NULL | `PENDING / PROCESSED / SKIPPED` |

索引:`(task_id)`。

### 2.7 batches

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| batch_id | TEXT | PK | |
| task_id | TEXT | NOT NULL, FK → tasks ON DELETE CASCADE | |
| batch_index | INTEGER | NOT NULL | 批次序号(游标) |
| status | TEXT | NOT NULL | `PENDING / PROCESSING / SUCCEEDED / FAILED / SKIPPED` |
| generated_item_ids | TEXT | NOT NULL DEFAULT '[]' | 本批 `generation_item_id` 列表(JSON) |
| retry_count | INTEGER | NOT NULL DEFAULT 0 | 重试计数 |
| coverage_rate / duplicate_rate | REAL | NULL | 整批质量(仅观测,FR-10) |
| difficulty_distribution / chapter_distribution / card_type_distribution | TEXT | NULL | JSON,同上 |
| cache_hit_tokens / cache_miss_tokens / output_tokens | INTEGER | NULL | Prompt Cache(FR-11) |
| created_at / ended_at | TEXT | 按需 | |

唯一约束:`UNIQUE (task_id, batch_index)`(游标完整性,断点续传依据)。

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
| position | INTEGER | NOT NULL | 牌组内排序位置,追加时分配 |
| front / back | TEXT | NOT NULL | 通用渲染字段 |
| code | TEXT | NULL | 卡片编号 |
| card_type | TEXT | NOT NULL | `QUESTION / TRUE_FALSE` |
| question / answer | TEXT | NULL | 仅 QUESTION 卡(决策 D-01) |
| statement / explanation | TEXT | NULL | 仅 TRUE_FALSE 卡 |
| answer_boolean | INTEGER | NULL | 仅 TRUE_FALSE 卡(0/1) |
| generation_item_id | TEXT | NULL | 仅 GENERATED 卡 |
| created_at / updated_at | TEXT | NOT NULL | |

约束与索引:

- **部分唯一索引**(SQLite 支持):`CREATE UNIQUE INDEX idx_cards_gen_item ON cards(generation_item_id) WHERE source = 'GENERATED' AND generation_item_id IS NOT NULL`(AC-05 重复入库率为 0)。
- 索引:`(deck_id, position)`;`(device_id, deck_id)`。

### 2.10 review_states

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| review_state_id | TEXT | PK | |
| card_id | TEXT | UNIQUE, FK → cards ON DELETE CASCADE | 与卡片一对一 |
| state | TEXT | NOT NULL | `NEW / LEARNING / REVIEW / RELEARNING`(FSRS) |
| stability | REAL | NOT NULL | FSRS 稳定性(天) |
| difficulty | REAL | NOT NULL | FSRS 难度 |
| due | TEXT | NOT NULL | 下次到期(服务端时间,ISO 8601 UTC) |
| last_review | TEXT | NULL | |
| reps | INTEGER | NOT NULL DEFAULT 0 | 复习次数 |
| lapses | INTEGER | NOT NULL DEFAULT 0 | 遗忘次数 |
| last_rating | TEXT | NULL | `AGAIN / HARD / GOOD / EASY` |
| updated_at | TEXT | NOT NULL | |

索引:`(due)`;`(card_id)`。

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
| idempotency_key | TEXT | | 请求头 `Idempotency-Key` |
| device_id | TEXT | NOT NULL | |
| path | TEXT | NOT NULL | 接口路径(不含查询参数) |
| response_status | INTEGER | NOT NULL | 首次成功响应状态码 |
| response_body | TEXT | NOT NULL | 首次成功响应体快照(JSON,重放用) |
| created_at | TEXT | NOT NULL | |

唯一约束:`UNIQUE (device_id, path, idempotency_key)`。

规则:

- 仅记录成功(2xx)响应;失败的重复请求直接重试执行。
- 重复请求:返回快照 `response_status + response_body`,不执行任何副作用。
- 保留策略:MVP 长期保留(数据量小);后续可加清理任务。

## 3. 级联与并发

| 删除对象 | 级联效果 |
| --- | --- |
| decks | cards → review_states、review_events 全部 CASCADE;任务保留 |
| pdf_files | chapters CASCADE;tasks.file_id SET NULL(任务与章节快照保留) |
| tasks | knowledge_points、batches CASCADE(本期无任务删除接口,预留) |
| cards | review_states、review_events CASCADE |

一致性规则:

- **连接配置**:每个连接开启 `PRAGMA foreign_keys=ON`(否则级联不生效);`PRAGMA journal_mode=WAL`(并发读 + 单写,满足 MVP 轮询 + 写并发场景)。
- **写事务**:SQLite 单写者;写操作使用 `BEGIN IMMEDIATE` 事务(进入即拿写锁)。事务边界:`create deck + cards`、`submit review event + update review_state`、`batch 完成 + 更新 task 游标` 各自在同一事务内;统计聚合可异步,但成功响应后最终一致(PRD 6.6)。
- **resume 并发防护**:`resume` 在 `BEGIN IMMEDIATE` 事务内先校验 `status == 'PAUSED' AND resumable = 1` 再置 `RUNNING`,状态校验失败返回 `TASK_STATE_CONFLICT`;由写锁串行化,无并发双跑风险。
- 单卡重写(FR-13,决策 C-05):原地更新 `cards` 行(新内容、新 `generation_item_id`,`position` 不变,`updated_at` / `version` 递增),`review_states` 重置为新卡初始状态;旧 `generation_item_id` 随列覆盖自然作废。
- `cards.device_id` 由服务端写入,保证与 `decks.device_id` 一致;所有资源归属校验一律按 `device_id` 过滤。

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
| knowledge_points | 3.6 KnowledgePoint |
| batches | 3.7 Batch |
| decks | 3.8 Deck |
| cards | 3.9 Card |
| review_states | 3.10 ReviewState |
| review_events | 3.11 ReviewEvent |
| (聚合计算) | 3.12 StatsDashboard |
| idempotency_keys | 总则 1.3 幂等约定 |
