# 数据库表设计 v2.5

持久化映射,字段定义源自 [structure-contract.md](structure-contract.md) 第 3 章资源模型;ORM 实现(`main/infra/db/`)必须与本设计一致。V2.5 目标设计见 [v2.5-target-architecture.md](v2.5-target-architecture.md) 第 5 章,迁移落地见本文件 §7.2。

## 0. 约定

- **数据库:SQLite**(MVP 选型,简单零运维;WAL 模式支持并发读 + 单写)。
- ORM 使用 SQLAlchemy(未来迁 PostgreSQL 时仅换 dialect,表语义与契约不变)。
- 类型映射:UUID → `TEXT`;时间 → `TEXT`(ISO 8601 UTC,与契约 1.2 一致,字符串比较即时间排序);JSON → `TEXT`;布尔 → `INTEGER(0/1)`;小数 → `REAL`;枚举 → `TEXT`。
- **时间格式唯一规范**:`YYYY-MM-DDTHH:MM:SS.sssZ`(UTC、零填充、恒 3 位毫秒),由统一序列化函数生成;禁止 `isoformat()` 默认输出(微秒省略、`+00:00` 偏移等变体)——混合格式会破坏 `due <= now` 范围比较与排序(审核修复)。
- **连接配置**(审核修复):`PRAGMA journal_mode=WAL;` 与 `PRAGMA foreign_keys=ON;`(SQLite 默认关闭外键)在 SQLAlchemy engine 级 connect 事件统一配置,覆盖池化连接、后台任务与迁移脚本;写事务用 `BEGIN IMMEDIATE`(engine `isolation_level='IMMEDIATE'`,进入即拿写锁,避免并发写直接 `SQLITE_BUSY`)。
- JSON 字段(`cursor`、`generated_item_ids`、`response_body` 等)统一经序列化函数写入,保证合法 JSON,禁止手工拼接。
- **数据主体隔离键(V2.2,决策 D-05)**:`user_id`。`users` / `auth_sessions` 表、直接归属 6 表(`pdf_files`、`tasks`、`decks`、`cards`、`review_events`、`llm_call_attempts`)的 `user_id` 列、`api_keys` / `idempotency_keys` 的 `user_id` 主键重建、`review_events` 另加 `UNIQUE (user_id, client_event_id)` 均已随数据地基迁移落地(见 7.1);V2.3 起设备架构彻底清除(见 7.1):`devices` 表、8 表 `device_id` 列与全部设备域约束/索引已物理删除,旧 device 域行随迁移删除,owner 恒为 `user_id`。所有业务表按隔离键的查询必须走索引。
- 幂等去重统一由 `idempotency_keys` 表承担(见 2.12),业务表不额外维护。

## 1. 实体关系概览

```text
users 1──N auth_sessions
users 1──1 user_preferences ──current_project── learning_projects
users 1──N pdf_files 1──1 materials(PDF) 1──N chapters
                     └────────────┴──N text_chunks
users 1──N learning_projects 1──N materials（资料集合权威归属;V25-D-29）
                            │        └─N chapters / text_chunks
                            │        └─PDF 资料 material_id == file_id（与 pdf_files 一对一）
                            1──1 project_study_settings
                            1──N project_study_decks ──N decks
                            1──N decks(project_id 可空=独立牌组)
                            1──N tasks ──N batches 1──1 knowledge_points
                                        └──N cards(source_task_id, STAGED/PUBLISHED)
users 1──N generation_operations ──0..1 tasks
                                 └──N llm_call_attempts
users 1──N decks 1──N cards 1──1 review_states
users 1──N review_events ──N cards
users 1──N llm_call_attempts
users 1──N card_deletion_batches ──N cards(delete_batch_id)
users 1──N card_rewrite_previews ──1 cards
users 1──1 api_keys（V2.2 主键重建）
users 1──N idempotency_keys（V2.2 主键重建）
```

注(V2.5):`users` 为根、`user_id` 为隔离键;V25-D-29 起项目是**资料集合**,资料归属权威 = `materials.project_id`(PDF 资料 `material_id == file_id`,解析状态/存储仍以 `pdf_files` 为权威;TEXT 资料无 pdf_files 行),`learning_projects` 不再持有 `file_id` 唯一外键,允许空项目(无资料,`EMPTY`);`Deck.project_id = null` 表示独立牌组;`Card.chapter_id = null` 表示"未归属章节"。

## 2. 表定义

> 类型均按 0 节映射规则;时间列默认 `TEXT NOT NULL`(ISO 8601 UTC);枚举列存储枚举字符串。
> **状态说明(V2.3)**:`users` / `auth_sessions` 与直接归属 6 表的 `user_id` 列、`api_keys` / `idempotency_keys` 的 `user_id` 主键重建、`review_events` 另加 `UNIQUE (user_id, client_event_id)` 已随数据地基迁移落地(见 7.1);2.1 devices 表节已随 V2.3 设备架构清除删除(见 7.1)。

### 2.2 api_keys

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| user_id | TEXT | NULL, PK, FK → users | 数据主体隔离键(V2.2,决策 D-05);一用户一 Key;新写入保证必填 |
| encrypted_key | TEXT | NOT NULL | 加密存储(决策 D-03),仅 `infra/llm/` 使用;算法 AES-256-GCM,随机 IV 随密文保存,解密密钥来自环境变量,不随数据库备份导出(审核修复) |
| status | TEXT | NOT NULL | `AVAILABLE / INVALID / INSUFFICIENT_BALANCE / UNKNOWN` |
| masked_key | TEXT | NOT NULL | 脱敏标识,如 `sk-****abcd` |
| updated_at | TEXT | NOT NULL | |

### 2.3 pdf_files

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| file_id | TEXT | PK | |
| user_id | TEXT | NULL, FK → users | 数据主体隔离键(V2.2,决策 D-05);新写入保证必填 |
| filename | TEXT | NOT NULL | |
| storage_key | TEXT | NOT NULL | 随机 UUID 存储路径,禁止含用户输入(filename 等);删除元数据时同步清理文件(契约 1.7,审核修复) |
| size_bytes | INTEGER | NOT NULL | |
| status | TEXT | NOT NULL | `PENDING / PARSING / PARSED / FAILED` |
| error_code | TEXT | NULL | `PDF_PARSE_FAILED / PDF_TOC_MISSING` |
| parse_lease_token | TEXT | NULL | 解析 worker 短租约令牌,仅事务外解析期间有效 |
| parse_lease_until | TEXT | NULL | 租约到期时间;过期后可重新领取 |
| parse_version | INTEGER | NOT NULL DEFAULT 0 | 删除/替换时递增的解析 fencing 版本 |
| created_at | TEXT | NOT NULL | |

索引:`(user_id, created_at)`(最近使用列表)。

### 2.4 chapters

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| chapter_id | TEXT | PK | |
| material_id | TEXT | NOT NULL, FK → materials ON DELETE CASCADE | V25-D-29 归属资料;章节随资料删除级联清理 |
| file_id | TEXT | NULL, FK → pdf_files ON DELETE CASCADE | PDF 资料章节 = material_id;TEXT 资料章节为 NULL |
| name | TEXT | NOT NULL | 用户可修改 |
| start_page | INTEGER | NULL | 用户可修改;TEXT 章节为 NULL(V25-D-32) |
| end_page | INTEGER | NULL | 用户可修改;TEXT 章节为 NULL(V25-D-32) |

索引:`(file_id)`、`(material_id)`。

### 2.5 tasks

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| task_id | TEXT | PK | |
| operation_id | TEXT | NULL, FK → generation_operations ON DELETE SET NULL | 稳定生成操作身份;同一用户 operation_key 跨请求/重启复用 |
| user_id | TEXT | NULL, FK → users | 数据主体隔离键(V2.2,决策 D-05);新写入保证必填 |
| project_id | TEXT | NULL, FK → learning_projects ON DELETE SET NULL | V2.5 归属项目;新写入保证必填,NULL 只兼容迁移前已失去 PDF 的终态任务(只读历史,不可重试) |
| file_id | TEXT | NULL, FK → pdf_files ON DELETE SET NULL | 删除 PDF 后任务保留,file_id 置空 |
| deck_id | TEXT | NULL, FK → decks ON DELETE SET NULL | 目标牌组(必须同项目);删除牌组后置空,任务保留(审核修复) |
| retry_of_task_id | TEXT | NULL, FK → tasks ON DELETE SET NULL | V2.5 只指向同用户失败任务 |
| status | TEXT | NOT NULL | V2.5 `DRAFT / SAMPLE_GENERATING / AWAITING_SAMPLE_CONFIRMATION / GENERATING / COMPLETED / FAILED / ABANDONED`(七态) |
| stage | TEXT | NULL | V2.5 改名 `internal_stage` 语义:`PLANNING / GENERATING / SCORING / PUBLISHING`,仅运行期内部观测 |
| selected_chapters | TEXT | NOT NULL | 章节快照(JSON,契约 3.4 Chapter[];每项含 `chapter_id/material_id/name/start_page/end_page`,TEXT 章节页码为 null),与源 chapter 解耦;开始正式生成前冻结快照 |
| generation_config | TEXT | NOT NULL | coverage_mode/难度整数比例/deep_question/自定义要求(JSON,契约 3.5) |
| sample_cards | TEXT | NULL | V2.5 持久化 1~3 张样卡(JSON);配置变化时清空 |
| sample_config_hash | TEXT | NULL | V2.5 样卡配置指纹,防止确认过期样卡 |
| sample_confirmed_at | TEXT | NULL | V2.5 样卡确认时间 |
| cursor | TEXT | NULL | `{ "completed_batch_count": int }`(JSON);游标为唯一源,与 `completed_batch_count` 列同事务原子写入 |
| generated_card_count | INTEGER | NOT NULL;应用层默认 0 | V2.5 只统计已发布卡;失败任务为 0 |
| total_batch_count | INTEGER | NULL | 规划完成后写入 |
| completed_batch_count | INTEGER | NULL | |
| completion_reason | TEXT | NULL | 空单元三分支:`NO_GENERATION_UNITS`(全组成功但 0 个合法单元,COMPLETED) |
| skipped_planning_group_count | INTEGER | NOT NULL DEFAULT 0 | 部分规划组失败被跳过的组数 |
| resumable | INTEGER | NOT NULL;应用层默认 0 | V2.5 内部租约恢复判定(只读观测字段,随 Task 响应返回;无 resume API) |
| failure_stage | TEXT | NULL | `PLANNING / GENERATING / SCORING / PUBLISHING` |
| error_code | TEXT | NULL | |
| claimed_by | TEXT | NULL | 当前 worker 标识;与 `lease_token` / `lease_until` 同时为空或同时非空 |
| lease_token | TEXT | NULL | 当前执行租约 fencing token;不对外暴露 |
| lease_version | INTEGER | NOT NULL DEFAULT 0 | 每次抢占/回收递增的 fencing 版本 |
| lease_until | TEXT | NULL | 租约绝对过期时间;过期后允许新的 worker 抢占 |
| attempt_count | INTEGER | NOT NULL DEFAULT 0 | 队列执行尝试次数 |
| next_attempt_at | TEXT | NULL | 退避后的最早下一次执行时间 |
| created_at / started_at / ended_at / updated_at | TEXT | 按需 | |

执行租约列:`claimed_by`、`lease_token`、`lease_version`、`lease_until`；队列观测/退避列:
`attempt_count`、`next_attempt_at`。三项租约指针必须同时为空或同时非空；worker 先以
`UPDATE ... WHERE status/stage AND (lease_until IS NULL OR lease_until <= now)` 原子抢占并
提交，再调用外部模型。所有结果写入带 token + version fencing，资源删除或租约回收会使旧
worker 的 CAS 失效。任务状态检查约束在 Alembic 迁移后只接受 V2.5 七态；`Base.metadata`
测试建表为兼容历史 fixture 额外接受旧 `PENDING/RUNNING/PAUSED`，这些值不得进入升级后的
生产库。

索引:`(user_id, created_at)`、`(project_id)`、`(status, stage, updated_at)`、
`(project_id, status, updated_at)`、`(deck_id, status, updated_at)`、
`(status, stage, next_attempt_at, lease_until, updated_at)`(`ix_tasks_queue_claim`,worker 队列领取扫描)、
`(operation_id)`(`ix_tasks_operation_id`)。

### 2.5.1 generation_operations

生成操作身份表，区分 HTTP 幂等缓存与跨请求/进程/重启的业务生成意图。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| operation_id | TEXT | PK | 服务端生成 |
| user_id | TEXT | NOT NULL, FK → users | 数据主体隔离 |
| operation_key | TEXT | NOT NULL, UNIQUE(user_id, operation_key) | 客户端稳定 operation/idempotency key |
| input_fingerprint | TEXT | NOT NULL | 归一化输入摘要,不保存原文 |
| status | TEXT | NOT NULL DEFAULT ACTIVE | `ACTIVE / COMPLETED / FAILED / ABANDONED` |
| task_id | TEXT | NULL | 关联任务快照;删除任务后置空,操作历史保留 |
| terminal_reason | TEXT | NULL | 失败/放弃/删除原因 |
| created_at / updated_at / ended_at | TEXT | 按需 | |

索引:`(user_id, status, updated_at)`；活跃输入指纹索引包含 `operation_key`，允许同一输入
的不同用户操作并存，同时禁止同一稳定 key 创建第二个任务。

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
| target_difficulty | TEXT | NULL | 规划锚定:`BASIC / UNDERSTANDING / DEEP_QUESTION`(V2.5 改名);旧数据无值,新数据保证必填;历史 `APPLICATION` 经迁移映射为 `DEEP_QUESTION` |
| card_type | TEXT | NULL | 规划锚定:`QUESTION / TRUE_FALSE`;旧数据无值,新数据保证必填 |
| coverage_tier | TEXT | NULL | Planner 覆盖层级:`CORE / IMPORTANT / LOW_FREQUENCY`(V25-D-26 分层口径、V25-D-27 注入生成 spec;历史行为 NULL) |
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
| generated_item_ids | TEXT | NOT NULL;应用层默认 '[]' | 本批 `generation_item_id` 列表(JSON;每单元 1 卡,成功时为单值) |
| retry_count | INTEGER | NOT NULL;应用层默认 0 | 重试计数(生成阶段兼容投影;尝试数与重试预算以 `llm_call_attempts` 为权威) |
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
| user_id | TEXT | NULL, FK → users | 数据主体隔离键(V2.2,决策 D-05);新写入保证必填 |
| project_id | TEXT | NULL, FK → learning_projects ON DELETE SET NULL | V2.5 归属项目;NULL = 手动/独立牌组 |
| name | TEXT | NOT NULL | |
| source | TEXT | NOT NULL | `MANUAL / IMPORTED / GENERATED`(V2.5 补 GENERATED) |
| version | TEXT | NOT NULL | 变更版本,客户端缓存刷新用 |
| created_at / updated_at | TEXT | NOT NULL | |

索引:`(user_id, updated_at)`、`(project_id)`。

### 2.9 cards

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| card_id | TEXT | PK | |
| deck_id | TEXT | NOT NULL, FK → decks ON DELETE CASCADE | |
| user_id | TEXT | NULL, FK → users | 数据主体隔离键(V2.2,决策 D-05);冗余列,服务端维护与 deck 一致 |
| source | TEXT | NOT NULL | `GENERATED / MANUAL / IMPORTED` |
| position | INTEGER | NOT NULL | 牌组内排序位置,追加时分配;`UNIQUE (deck_id, position)`(审核修复) |
| front / back | TEXT | NOT NULL | 通用渲染字段 |
| code | TEXT | NULL | 卡片编号 |
| card_type | TEXT | NOT NULL | `QUESTION / TRUE_FALSE` |
| question / answer | TEXT | NULL | 仅 QUESTION 卡(决策 D-01) |
| statement / explanation | TEXT | NULL | 仅 TRUE_FALSE 卡 |
| answer_boolean | INTEGER | NULL | 仅 TRUE_FALSE 卡(0/1) |
| generation_item_id | TEXT | NULL | 仅 GENERATED 卡 |
| source_task_id | TEXT | NULL, FK → tasks ON DELETE SET NULL | V2.5 生成来源任务;删历史保留卡时置空 |
| chapter_id | TEXT | NULL, FK → chapters ON DELETE SET NULL | V2.5 源章节;null = 未归属章节 |
| publication_state | TEXT | NOT NULL DEFAULT 'PUBLISHED' | V2.5 `STAGED / PUBLISHED`;历史卡迁为 PUBLISHED |
| delete_batch_id | TEXT | NULL, FK → card_deletion_batches ON DELETE SET NULL | V2.5 非空 = 10 秒待删除批次 |
| pending_delete_at | TEXT | NULL | V2.5 服务端计时 |
| undo_until | TEXT | NULL | V2.5 服务端撤销窗口 |
| target_difficulty | TEXT | NULL | 仅 GENERATED 卡;`BASIC / UNDERSTANDING / DEEP_QUESTION`(V2.5 改名);手动/导入为 null |
| knowledge_point_ids | TEXT | NULL | 仅 GENERATED 卡;JSON 数组,综合应用卡可多个(审核修复) |
| evidence_score / correctness_score / difficulty_score / learning_value_score | INTEGER | NULL | Rubric 各维度 0~3,仅 GENERATED 卡(审核修复) |
| rubric_total_score | INTEGER | NULL | Rubric 总分 0~12,仅 GENERATED 卡 |
| version | TEXT | NOT NULL | 变更版本,重写时递增(审核修复) |
| created_at / updated_at | TEXT | NOT NULL | |

约束与索引:

- **部分唯一索引**(SQLite 支持):`CREATE UNIQUE INDEX ix_cards_gen_item_partial ON cards(generation_item_id) WHERE source = 'GENERATED' AND generation_item_id IS NOT NULL`(AC-05 重复入库率为 0)。
- 唯一索引:`UNIQUE (deck_id, position)`(追加 position 分配的并发保护)。
- 索引:`(user_id, deck_id)`、`(source_task_id)`、`(chapter_id)`、`(publication_state, delete_batch_id)`(统一可见谓词,契约 3.9)。

**统一可见谓词(V2.5)**:`publication_state = 'PUBLISHED' AND delete_batch_id IS NULL`,所有列表、到期队列、今日计划、统计与进度聚合复用同一查询条件,禁止各模块自行漏写过滤。

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
| reps | INTEGER | NOT NULL;应用层默认 0 | 复习次数 |
| lapses | INTEGER | NOT NULL;应用层默认 0 | 遗忘次数 |
| last_rating | TEXT | NULL | `AGAIN / HARD / GOOD / EASY` |
| updated_at | TEXT | NOT NULL | |

索引:`(due)`(`card_id` 由 UNIQUE 自带索引,无需独立索引,审核修复)。

### 2.11 review_events

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| review_event_id | TEXT | PK | |
| user_id | TEXT | NULL, FK → users | 数据主体隔离键(V2.2,决策 D-05);新写入保证必填 |
| card_id | TEXT | NOT NULL, FK → cards ON DELETE CASCADE | |
| client_event_id | TEXT | NOT NULL | 客户端生成 |
| rating | TEXT | NOT NULL | `AGAIN / HARD / GOOD / EASY` |
| reviewed_at | TEXT | NOT NULL | 服务端时间 |
| device_timezone | TEXT | NULL | V2.5 降级为可空审计字段,不参与权威统计 |
| created_at | TEXT | NOT NULL | |

约束与索引:

- 唯一约束:`UNIQUE (user_id, client_event_id)`(离线重试去重,AC-10)。
- 索引:`(user_id, reviewed_at)`(看板聚合);`(card_id)`。

不可变记录:不提供 UPDATE / DELETE。

### 2.12 idempotency_keys

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| user_id | TEXT | NULL, 复合主键 | 数据主体隔离键(V2.2,决策 D-05);新写入保证必填 |
| path | TEXT | 复合主键 | 接口路径**含具体资源 ID**(`/v1/pdfs/<file_id>`),避免同一用户对不同资源复用同键被静默吞掉(审核修复) |
| idempotency_key | TEXT | 复合主键 | 请求头 `Idempotency-Key` |
| response_status | INTEGER | NOT NULL | 首次成功响应状态码 |
| response_body | TEXT | NOT NULL | 首次成功响应体快照(JSON,重放用) |
| request_body_hash | TEXT | NOT NULL | 首次请求体 SHA-256 摘要(hex)；幂等键相同但摘要与首次不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3 比对的持久化载体,审核补全) |
| created_at | TEXT | NOT NULL | |

主键:`PRIMARY KEY (user_id, path, idempotency_key)`(V2.2 主键重建;SQLite 非 INTEGER 主键允许 NULL)。

规则:

- 仅记录成功(2xx)响应;失败的重复请求直接重试执行。
- 重复请求:返回快照 `response_status + response_body`,不执行任何副作用;**幂等记录 INSERT 与业务副作用必须在同一事务内**(响应丢失后同键重试不双写,AC-05/AC-10)。
- 请求体一致性:重复请求携带相同 `Idempotency-Key` 时,比对 `request_body_hash` 与首次记录;不一致 → `409 IDEMPOTENCY_CONFLICT`(契约 1.3)。
- 保留策略:TTL 90 天(幂等窗口只需覆盖客户端重试周期),清理任务定期删除过期记录(审核修复)。

### 2.13 text_chunks

按资料持久化文本(LLM 链路升级工作包新增;V25-D-29/32 多资料化):scanner PARSED 时完整解析每页文本,**一页一行、与章节解耦**——不保存 `chapter_id`,不在扫描阶段按章节切块;章节名称/页码可在 PARSED 后修改,页文本不随章节编辑、删除而重建。TEXT 资料(V25-D-32)按段落切分为 `chunk_seq` 1..N 伪页码行。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| chunk_id | TEXT | PK | 服务端按 `(归属ID, 序号, content_sha256)` 确定性生成;同一内容标识稳定 |
| material_id | TEXT | NOT NULL, FK → materials ON DELETE CASCADE | V25-D-29 权威归属;删除资料时级联清理全文数据 |
| file_id | TEXT | NULL, FK → pdf_files ON DELETE CASCADE | PDF 资料块 = material_id;TEXT 资料块为 NULL |
| chunk_seq | INTEGER | NOT NULL | PDF 块 = page_number;TEXT 块 = 1..N 伪页码(V25-D-32) |
| page_number | INTEGER | NOT NULL | 页码(一页一行;TEXT 资料同 chunk_seq) |
| char_count | INTEGER | NOT NULL | 页字符数(规划分组/配额依据) |
| content_sha256 | TEXT | NOT NULL | 页文本摘要(输入指纹/漂移检测依据) |
| content | TEXT | NOT NULL | 完整页文本(功能数据;完整 PDF 文本/完整 Prompt/原文样例仍禁止写日志、审计与调用账本) |
| created_at | TEXT | NOT NULL | |

唯一约束:`UNIQUE (material_id, chunk_seq)`(V25-D-29 取代旧 `(file_id, page_number)`)。索引:`(material_id, chunk_seq)`。
重解析幂等:先清理该 material_id 的既有页文本再重建;章节范围内所有页均无有效文本时该章不发 Planner 请求,作为成功空结果处理。

### 2.14 llm_call_attempts

LLM 调用账本(LLM 链路升级工作包新增):**重试预算、调用上限与全阶段 token 的权威**。任何外部 chat 调用必须先有已提交的 STARTED 占位行(调用前持久化),Task 上不设冗余计数列。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| call_id | TEXT | PK | |
| user_id | TEXT | NULL, FK → users | 数据归属(V2.2,决策 D-05);新写入保证必填 |
| scope_type / scope_id | TEXT | NOT NULL | `TASK` / `CARD`;任务链路 scope_id=task_id,单卡重写 scope_id=card_id |
| task_id | TEXT | NULL, FK → tasks ON DELETE SET NULL | 可空;删除任务时先解除引用以保留账本(实际库存在 CASCADE 已知偏差,见下) |
| operation_id | TEXT | NULL, FK → generation_operations ON DELETE SET NULL | 跨阶段操作归属 |
| stage | TEXT | NOT NULL | `SAMPLE / PLANNING / GENERATING / SCORING / REWRITE` |
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
| status | TEXT | NOT NULL;应用层默认 'STARTED' | `STARTED / SUCCESS / FAILED / UNKNOWN` |
| error_code | TEXT | NULL | 失败类别 |
| normalized_result | TEXT | NULL | PLANNING 成功时保存规范化 units JSON、SAMPLE 成功时保存规范化样卡 JSON;不保存原文、完整 Prompt 或原始模型响应 |
| created_at / finished_at | TEXT | 按需 | 调用占位与结束时间 |

唯一约束:`UNIQUE (scope_type, scope_id, stage, operation_key, attempt_no)`。
索引:`(user_id, created_at)`、`(task_id, stage, operation_key)`、
`(operation_id, stage, operation_key)`(`ix_llm_call_attempts_operation`)。

> **已知偏差(外键级联)**:历史迁移 `0003_llm_pipeline_upgrade` 将 `task_id` 外键实际建为
> `ON DELETE CASCADE`,后续迁移 `c5d6e7f8a9b0` 保留未重建;设计语义为本表的 SET NULL
> (ORM `Base.metadata` 测试建表即 SET NULL,与生产迁移库存在差异)。删除服务在同一写事务内
> 先把 `task_id` 置空再删除任务行,账本因此保留;但绕过应用层的裸 SQL 删除任务会级联删掉
> 账本行。待后续迁移统一重建为 SET NULL 后移除本注记。

规则:

- 重试判定:该 operation_key 的 STARTED/SUCCESS/FAILED/UNKNOWN 尝试总数达到预算 → 不再发请求;孤儿 STARTED 转 UNKNOWN 并计数,不能仅统计 FAILED。
- 同一 `operation_key + input_fingerprint` 最多一个 SUCCESS;领域写入与调用终态在同一事务提交,禁止账本已 SUCCESS 但业务结果未落库。
- Rewrite 的 operation_key 必须区分同一卡片的多次用户请求(不得用 `rewrite:{card_id}` 让历史失败影响后续合法重写)。
- Scoring 上限:账本 stage=SCORING 的全部尝试数 ≤ `max_scoring_calls_per_task`(抽样预保证 + 调用前账本条件校验)。
- 成本口径:账本是 Planner/Generator/Scoring/Rewrite 总 token 的唯一来源;Batch token 仅是 GENERATING 的兼容投影,不得再次相加双计。

### 2.15 users

账号数据主体(V2.2,决策 D-05):`user_id` 为数据主体隔离键,替代 v2.1 匿名设备隔离。
(V2.4:登录键切换为 email,username 降为展示名。V2.5:增加预设头像。)

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| user_id | TEXT | PK | 服务端生成 |
| username | TEXT | NOT NULL | 展示名:1~24 位,中文/字母/数字/._-,可重名(无 UNIQUE) |
| email | TEXT | NOT NULL | 登录键;服务端转小写规范化;UNIQUE(uq_users_email) |
| avatar_key | TEXT | NOT NULL DEFAULT 'mood_01' | V2.5 预设头像:`mood_01`~`mood_12`,只接受内置预设 |
| password_hash | TEXT | NOT NULL | Argon2id 输出(生产参数 ≥ `memory_cost=19456 KiB, time_cost=2, parallelism=1`);绝不进入日志/响应 |
| created_at / updated_at | TEXT | NOT NULL | |

唯一约束:`UNIQUE (email)`(约束名 uq_users_email)。

### 2.16 auth_sessions

登录会话(V2.2):token 为 256-bit 不透明随机串,库内只存其 SHA-256 摘要(`token_hash`),绝不存明文。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| session_id | TEXT | PK | 服务端生成 |
| user_id | TEXT | NOT NULL, FK → users ON DELETE CASCADE | 归属用户;删除用户级联清理会话 |
| token_hash | TEXT | UNIQUE NOT NULL | 256-bit opaque token 的 SHA-256 摘要 |
| created_at | TEXT | NOT NULL | |
| expires_at | TEXT | NOT NULL | 绝对有效期 30 天；V2.4 起活跃滑动续期（见 6.11） |
| revoked_at | TEXT | NULL | logout 只撤销当前会话 |

唯一约束:`UNIQUE (token_hash)`。索引:`(user_id)`。
有效判定:`revoked_at IS NULL AND expires_at > now`。
V2.4 起 `expires_at` 支持滑动续期(活跃续期至 now+30 天,见 structure-contract 6.11)。

### 2.17 learning_projects（V2.5 新增）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| project_id | TEXT | PK | 服务端生成 |
| user_id | TEXT | NOT NULL, FK → users | 数据主体隔离键 |
| name | TEXT | NOT NULL | 去首尾空白后 1~60 字符,可重名;两步创建第一步由请求体 `name` 提供 |
| chapters_confirmed_at | TEXT | NULL | 目录确认时间;`status` 由全部资料状态与本列聚合派生(契约 3.16,不建第二套状态列) |
| version | TEXT | NOT NULL | 缓存刷新与并发检查 |
| created_at / updated_at | TEXT | NOT NULL | |

索引:`(user_id, updated_at)`。
V25-D-29 起不再持有 `file_id` 唯一外键:资料归属权威 = `materials.project_id`,允许空项目(删最后一份资料后项目存活,status=`EMPTY`);新增/删除任一资料重置 `chapters_confirmed_at`(V25-D-31)。

### 2.18 user_preferences（V2.5 新增）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| user_id | TEXT | PK, FK → users | 一用户一行 |
| coverage_mode | TEXT | NOT NULL DEFAULT 'BALANCED' | `COMPACT / BALANCED / EXTENSIVE` |
| basic_ratio | INTEGER | NOT NULL DEFAULT 40 | 10% 整数档 0~100 |
| understanding_ratio | INTEGER | NOT NULL DEFAULT 40 | 10% 整数档 0~100 |
| deep_question_ratio | INTEGER | NOT NULL DEFAULT 20 | 10% 整数档 0~100 |
| daily_goal | INTEGER | NOT NULL DEFAULT 50 | 10~200,10 的倍数 |
| learning_timezone | TEXT | NOT NULL | 有效 IANA 时区,账号级权威 |
| current_project_id | TEXT | NULL, FK → learning_projects ON DELETE SET NULL | 项目删除时置空 |
| updated_at | TEXT | NOT NULL | 最后成功保存时间 |

约束:`CHECK (basic_ratio % 10 = 0 AND basic_ratio BETWEEN 0 AND 100)`、同型 CHECK × 3、`CHECK (basic_ratio + understanding_ratio + deep_question_ratio = 100)`、`CHECK (daily_goal BETWEEN 10 AND 200 AND daily_goal % 10 = 0)`。

### 2.19 project_study_settings（V2.5 新增）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| project_id | TEXT | PK, FK → learning_projects ON DELETE CASCADE | 一项目一行 |
| selected_chapter_ids | TEXT | NOT NULL DEFAULT '[]' | 新卡章节范围(JSON);空数组 = 暂无新卡范围 |
| include_unassigned | INTEGER | NOT NULL DEFAULT 0 | 是否包含 `chapter_id = null` 的新卡(0/1) |
| daily_new_goal | INTEGER | NOT NULL DEFAULT 10 | 每日新学目标,0~200 且为 10 的倍数 |
| daily_review_goal | INTEGER | NOT NULL DEFAULT 40 | 每日巩固目标,0~200 且为 10 的倍数 |
| updated_at | TEXT | NOT NULL | |

约束:`CHECK (daily_new_goal BETWEEN 0 AND 200 AND daily_new_goal % 10 = 0)`、
`CHECK (daily_review_goal BETWEEN 0 AND 200 AND daily_review_goal % 10 = 0)`、
`CHECK (daily_new_goal + daily_review_goal > 0)`。

### 2.19.1 project_study_decks（V2.5 新增）

今日学习计划选中的卡组关联表。卡组必须属于同一用户且已归属该项目；删除项目或卡组时
关联行级联删除。计划只保存卡组 ID，不再把章节 ID JSON 当作今日计划范围。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| project_id | TEXT | 复合主键, FK → learning_projects ON DELETE CASCADE | |
| deck_id | TEXT | 复合主键, FK → decks ON DELETE CASCADE | |
| created_at | TEXT | NOT NULL | 选择进入计划的时间 |

索引:`(deck_id)`。

### 2.20 card_deletion_batches（V2.5 新增）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| delete_batch_id | TEXT | PK | 服务端生成 |
| user_id | TEXT | NOT NULL, FK → users | 数据主体隔离键 |
| status | TEXT | NOT NULL | `PENDING / UNDONE / FINALIZED` |
| undo_until | TEXT | NOT NULL | 服务端接受最后一次追加后 10 秒 |
| created_at / updated_at | TEXT | NOT NULL | |

索引:`(user_id, status, undo_until)`。
规则:向仍为 `PENDING` 的批追加卡时,服务端原子更新整批 `undo_until = now + 10s`;撤销在同一事务清空所有卡片删除标记并置 `UNDONE`;过期批由后台清理器或任意相关读取前的惰性清理最终硬删除,两种路径必须幂等。

### 2.21 card_rewrite_previews（V2.5 新增）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| rewrite_id | TEXT | PK | 服务端生成 |
| user_id | TEXT | NOT NULL, FK → users | 数据主体隔离键 |
| card_id | TEXT | NOT NULL, FK → cards ON DELETE CASCADE | |
| base_card_version | TEXT | NOT NULL | 应用时乐观并发校验(CAS) |
| preview | TEXT | NOT NULL | 预览内容 JSON(front/back/card_type/target_difficulty) |
| custom_requirements | TEXT | NULL | 不保存完整 Prompt |
| status | TEXT | NOT NULL DEFAULT 'PENDING' | `PENDING / APPLIED / CANCELLED / EXPIRED` |
| expires_at | TEXT | NOT NULL | 24 小时(实现常量统一) |
| created_at / updated_at | TEXT | NOT NULL | |

索引:`(user_id, status, expires_at)`(pending expiry 清理)。

### 2.22 materials（V2.5 多资料新增,V25-D-29）

学习项目 = 资料集合(契约 3.2a):本表承载资料归属与摘要。PDF 资料行与 `pdf_files` 一对一(`material_id == file_id`),解析状态/存储/租约以 `pdf_files` 为权威,本表 `status` 置 NULL 防第二套状态漂移;TEXT 资料行(V25-D-32)无 `pdf_files` 行,`status` 恒 `READY`,内容经 chapters + text_chunks(`chunk_seq` 伪页码)承载。

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| material_id | TEXT | PK | PDF 资料 = `pdf_files.file_id`;TEXT 资料服务端生成 |
| project_id | TEXT | NOT NULL, FK → learning_projects ON DELETE CASCADE | 资料归属权威;项目删除级联清理资料 |
| type | TEXT | NOT NULL | `PDF / TEXT`(LINK 预留) |
| name | TEXT | NOT NULL | PDF=文件名;TEXT=用户可改标题(1~60 字符) |
| status | TEXT | NULL | PDF 行恒 NULL(权威在 `pdf_files.status`);TEXT 行恒 `READY` |
| error_code | TEXT | NULL | 仅 PDF 解析失败码 |
| size_bytes | INTEGER | NULL | 仅 PDF |
| char_count | INTEGER | NULL | 仅 TEXT;1~30000 |
| created_at | TEXT | NOT NULL | |

索引:`(project_id, created_at)`(资料列表与状态聚合)。
删除语义(V25-D-30):资料级删除走本表行,chapters/text_chunks 经 FK 级联;PDF 资料连带删除 `pdf_files` 行与存储对象;`retain_cards` 决定该资料产出卡片去留。删最后一份资料后项目存活(`EMPTY`),增删均重置 `chapters_confirmed_at`(V25-D-31)。

## 3. 级联与并发

| 删除对象 | 级联效果 |
| --- | --- |
| users | auth_sessions CASCADE(本期无用户删除接口,预留) |
| learning_projects | materials CASCADE(chapters/text_chunks 随资料级联);project_study_settings/project_study_decks CASCADE;user_preferences.current_project_id SET NULL;decks.project_id SET NULL;tasks.project_id SET NULL;PDF 资料连带删 pdf_files 行与存储对象,删除确认时服务端先自动 CAS 取消全部活跃任务 |
| materials | chapters/text_chunks CASCADE;PDF 资料级联删 pdf_files 行;cards.chapter_id 随章节删除 SET NULL 或按用户选择删除;引用该资料的活跃任务静默取消(V25-D-30) |
| decks | cards → review_states、review_events 全部 CASCADE;tasks.deck_id SET NULL;删除确认时服务端先自动 CAS 取消全部活跃任务 |
| pdf_files | chapters CASCADE;tasks.file_id SET NULL;项目删除确认时服务端先自动 CAS 取消全部活跃任务 |
| tasks | knowledge_points、batches CASCADE;cards.source_task_id SET NULL(保留卡)或按用户选择删除其已发布卡(5.3) |
| cards | review_states、review_events、card_rewrite_previews CASCADE |
| card_deletion_batches | cards.delete_batch_id SET NULL |

一致性规则:

- **连接配置**:每个连接开启 `PRAGMA foreign_keys=ON`(否则级联不生效);`PRAGMA journal_mode=WAL`(并发读 + 单写,满足 MVP 轮询 + 写并发场景)。两者在 SQLAlchemy engine 级 connect 事件统一配置,覆盖池化连接、后台任务与迁移脚本。
- **写事务**:SQLite 单写者;写操作使用 `BEGIN IMMEDIATE` 事务(engine `isolation_level='IMMEDIATE'`,进入即拿写锁)。事务边界:`create deck + cards`、`submit review event + update review_state`、`batch 完成 + 更新 task 游标`、**幂等记录 INSERT + 业务副作用** 各自在同一事务内;统计聚合可异步,但成功响应后最终一致(PRD 6.6)。
- **任务租约并发防护**:worker 以 `BEGIN IMMEDIATE` 下的条件 UPDATE 抢占 `(status, stage)`，提交 `lease_token/version` 后才调用 LLM；每批/每组心跳续租，过期租约回收时递增 version。所有终态、样卡、发布和删除写入带原 token/version 的 CAS，旧 worker 只能得到 0 行更新，不得覆盖或复活已取消任务。
- 新建卡片时同事务插入初始 `review_states`(state=NEW,初始排程参数,审核修复)。
- 单卡重写(FR-13,决策 C-05):原地更新 `cards` 行(新内容、新 `generation_item_id`,`position` 不变,`updated_at` / `version` 递增),`review_states` 重置为新卡初始状态;旧 `generation_item_id` 随列覆盖自然作废。
- **V2.5 原子事务(5.3)**,必须在同一事务完成:
  - 正式任务成功:校验至少一张合法 STAGED 卡 → 全部改 PUBLISHED → task COMPLETED/generated_card_count 更新;
  - 任意正式阶段失败:task FAILED,STAGED 卡继续隔离;
  - 项目删除:状态保护 → retain 选择对应 detach 或 delete → 删除 PDF/章节/任务/项目;
  - 卡片删除批追加、撤销和最终清理;
  - 重写预览应用:版本 CAS → 卡片正文更新 → ReviewState 重置 → preview APPLIED;
  - 评级:ReviewEvent 插入 → ReviewState 更新;今日/周完成动态聚合,不另存易漂移计数。
- **V2.5 LLM 调用不持有 SQLite 长写事务**:规划/生成/评分 LLM 调用发生在事务之外,事务只包短状态/发布更新(风险 R25-07);调用账本 STARTED 占位与领域写入按既有规则同事务提交。
- **删除原子性**:预检只读且不保留锁；真正删除在同一 `BEGIN IMMEDIATE` 事务内重新读取活跃任务。项目或卡组确认删除时，服务端先 CAS 取消全部活跃任务、标记 STARTED 为 UNKNOWN、复位 PROCESSING 批次并关闭 operation，再删除资源；客户端不再提供任务处理选项。LLM 账本保留用于成本对账，任务历史删除前解除其外键引用。
- `cards.user_id` 由服务端写入,保证与 `decks.user_id` 一致;数据主体隔离键为 `user_id`(V2.2);(V2.1 历史:v2.1 归属校验按 `device_id` 过滤,已随 V2.3 设备架构清除删除;无隔离键列的表如 chapters、knowledge_points、batches、review_states 经 FK join 到所属隔离键)。

## 4. 看板聚合实现说明

MVP 直接基于 `review_events` 聚合(索引 `(user_id, reviewed_at DESC)` 已覆盖),不建物化聚合表。统计口径见 PRD 5.16 与结构契约 3.12。单用户数据量(千级卡片、万级事件)下 SQLite 聚合毫秒级完成,若后续数据量增长再引入异步聚合(PRD 6.6 允许)。

## 5. 迁移路径(未来,不在本期实现)

若上线后需要多实例部署或跨设备同步,迁移 PostgreSQL:

- ORM 层已用 SQLAlchemy,换 dialect 即可;表结构与契约语义不变。
- 需调整:时间列 TEXT → `timestamptz`、JSON 列 TEXT → `jsonb`、`resume` 并发防护改用 `SELECT ... FOR UPDATE`、迁移工具生成 DDL。
- 接口契约(`openapi.yaml`)不受影响。

## 6. 与结构契约的映射

| 表 | 契约资源 |
| --- | --- |
| api_keys | 3.1 ApiKey |
| users / auth_sessions | 3.14 AuthUser / 3.24 AuthSessionResponse(V2.2,已随数据地基迁移落地) |
| pdf_files / chapters | 3.2 PdfFile / 3.3 Chapter |
| materials | 3.2a Material(V2.5 多资料新增;PDF 资料 material_id == file_id) |
| tasks / generation_config | 3.4 GenerationTask / 3.5 GenerationConfig |
| knowledge_points | 3.6 KnowledgePoint(生成单元) |
| batches | 3.7 Batch |
| decks | 3.8 Deck |
| cards | 3.9 Card |
| review_states | 3.10 ReviewState |
| review_events | 3.11 ReviewEvent |
| (聚合计算) | 3.12 StatsDashboard / 3.20 TodayStudyPlan |
| idempotency_keys | 总则 1.3 幂等约定 |
| text_chunks | 3.6 KnowledgePoint 来源页底座(来源分片标识) |
| llm_call_attempts | 3.7 Batch / 8.5 评估骨架(调用账本) |
| learning_projects | 3.16 LearningProject(V2.5 新增) |
| user_preferences | 3.15 UserPreferences(V2.5 新增) |
| project_study_settings | 3.17 ProjectStudySettings(V2.5 新增) |
| project_study_decks | 3.17.1 卡组计划选择(2.19.1;由 `f7a2b3c4d5e6` 迁移创建) |
| card_deletion_batches | 3.18 CardDeletionBatch(V2.5 新增) |
| card_rewrite_previews | 3.19 CardRewritePreview(V2.5 新增) |

## 7. 演进路径

### 7.1 账号体系（V2.2，数据地基迁移已落地）

账号体系(决策 D-05)引入 `users` / `auth_sessions`,数据主体隔离键从 `device_id` 切换为 `user_id`。
决策 D-06:旧 device_id 数据**不迁移、不认领、无访问路径**;`devices` 与旧 `device_id` 列降级为仅兼容
审计,不参与认证/授权。

**已落地(数据地基迁移,§0/§1/§2/§3 与 ORM 同批更新)**:

- 新增表 `users`、`auth_sessions`(定义见 2.15/2.16)。
- 直接归属 6 表(`pdf_files`、`tasks`、`decks`、`cards`、`review_events`、`llm_call_attempts`)补 `user_id` 列 + `(user_id, …)` 查询索引;旧 `device_id` 降级为可空遗留列,双非空 CHECK(`device_id IS NOT NULL OR user_id IS NOT NULL`)保证二者至少其一(未认领历史行只有 device_id,新行只有 user_id;应用与测试保证新写入不再生成 device_id)。
- 外键传递归属不变:chapters/text_chunks 经 PDF,knowledge_points/batches 经 Task,review_states 经 Card。

**已落地(主键重建任务)**:

- `api_keys` PK 重建为 `user_id`(一用户一 Key,`TEXT NULL PK + FK → users`),并补回 `UNIQUE (device_id)`(v2.1 每设备唯一性保障随 PK 重建丢失,遗留设备域防重;用户域行 device_id NULL 多行不冲突——SQLite UNIQUE 对 NULL 视为互异);`idempotency_keys` 主键重建为 `PRIMARY KEY (user_id, path, idempotency_key)`,保留遗留 `UNIQUE (device_id, path, idempotency_key)`;`review_events` 另加 `UNIQUE (user_id, client_event_id)`,原 `UNIQUE (device_id, client_event_id)` 保留(旧设备域幂等缓存不跨身份空间重放)。
- `api_keys`/`idempotency_keys` 两表 `device_id` 降级为可空遗留列(旧行原值保留、`user_id` 为 NULL),加双非空 `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`(`review_events` 的降级与 CHECK 已随数据地基迁移 ddc6f34e30b8 在直接归属 6 表中落地,不属本任务)。
- downgrade fail-closed 预检(自 a7cc699f3fd8 起生效):降级前检查 `users` 计数与各 owner 表 `user_id IS NOT NULL` 计数,存在用户域数据即在任何 DDL/DML 前抛异常拒绝(不丢弃新数据、不合成 device_id);空库/纯旧 device 域数据副本允许正常降级(旧行保留)。

**迁移策略**(Alembic):

- 运行时读取真实 Alembic head 后创建下一 revision(不预写文件名);SQLite 重建约束用 batch 操作,显式检查外键/索引/级联;batch 重建 FK 父表期间关闭外键强制,避免 DROP 旧表隐式 DELETE 级联误删子表数据。
- 在临时空库与尚未产生账号写入的旧库副本上验证 upgrade → downgrade → upgrade。
- 物理删除旧列/legacy 表已由 V2.3 完成(2026-08-14,不可逆)。

**V2.3 落地(2026-08-14,设备架构彻底清除,决策翻转 D-06→V2.3)**:

- 新 revision(down_revision = e85c78b2a345):旧 device 域行(`user_id IS NULL`)按子表先行序物理删除 → 8 个双非空 CHECK 删除 → 3 个 device 版 UNIQUE(`uq_idempotency_keys_device_path` / `uq_api_keys_device_id` / `uq_review_events_device_client`)删除 → 6 个 device_ 索引删除 → 8 表 `device_id` 列删除 → `devices` 表 drop。
- 删除不可逆:`downgrade` 第一行 `raise RuntimeError`(延续 fail-closed 精神,不假装可回滚);回退仅限恢复升级前备份。
- owner 恒为 `user_id`;`review_events.device_timezone` 为复习事件负载字段(看板分桶),保留。
- §0/§1/§2/§3/§4/§6 与 ORM 同批更新(见 PRD V2.3)。

**V2.4 落地(2026-08-14,email 登录键)**:

- 新 revision(ad7849aad10e,down_revision = b92357b079ca):12 张 user 域下游表 + `auth_sessions` + `users` 全量清空(用户裁决:存量测试账号清空重来)→ `users.username` 去唯一(drop `uq_users_username`)→ `users` 加 `email` 列(NOT NULL,登录键,约束 `uq_users_email`,服务端转小写规范化)。
- 删除不可逆:`downgrade` 第一行 `raise RuntimeError`(fail-closed,延续 V2.3 精神);回退仅限恢复升级前备份。
- §2.15/§2.16 与 ORM 同批更新(见 PRD V2.4)。

### 7.2 新卡类型（未来）

- 沿用 D-01 模式:专用列 + `front`/`back` 通用渲染。
- 类型数可控(≤5)时继续用专用列;字段高度异构或继续膨胀时,评估 JSON 扩展列方案。
- 所有结构变更走迁移工具。

### 7.3 V2.5 落地(2026-08-15,学习项目与整批发布;不可逆)

V2.5 使用一个**新的不可逆 Alembic revision**;迁移从运行时真实 head 生成,不预写 revision ID。升级前备份数据库,downgrade 继续 fail-closed。不得清空 V2.4 用户数据。

- **新表**:`learning_projects`、`user_preferences`、`project_study_settings`、`project_study_decks`、`card_deletion_batches`、`card_rewrite_previews`(定义见 2.17~2.21)。
- **现有表调整**:
  - `users`:新增 `avatar_key NOT NULL DEFAULT 'mood_01'`。
  - `decks`:新增 `project_id NULL FK → learning_projects ON DELETE SET NULL`;source 枚举补 `GENERATED`。
  - `tasks`:新增 `project_id NULL FK`(新写入必填,NULL 只兼容迁移前已失去 PDF 的终态任务)、`retry_of_task_id NULL FK SET NULL`、`sample_cards`/`sample_config_hash`/`sample_confirmed_at`;状态迁移 `PENDING→DRAFT`、`RUNNING→GENERATING`、`COMPLETED→COMPLETED`、`FAILED→FAILED`、`CANCELLED→ABANDONED`;历史 `PAUSED` 迁为 `FAILED` 并写 `error_code=LEGACY_PAUSED_TASK`,禁止留下 V2.5 不可表达状态。
  - `cards`:新增 `source_task_id NULL FK tasks SET NULL`、`chapter_id NULL FK chapters SET NULL`、`publication_state NOT NULL DEFAULT 'PUBLISHED'`、`delete_batch_id NULL FK card_deletion_batches SET NULL`、`pending_delete_at`/`undo_until`;历史卡均迁为 `PUBLISHED`。
  - `knowledge_points`/`cards` 历史 `target_difficulty='APPLICATION'` 映射为 `DEEP_QUESTION`。
  - `review_events.device_timezone`:改为可空审计字段;不删除历史值。
- **回填**:每个现有 PDF 建一个学习项目,名称取 filename 去扩展名;`PARSED` PDF 项目默认 `chapters_confirmed_at = migrated_at`,其他按 PDF 状态映射;既有 generated deck 若能从 task.file_id 唯一定位项目则绑定,无法唯一定位的牌组保持独立;现有 task 绑定其 file 对应项目;`file_id = null` 的既有终态任务保留为只读历史且不可重试。迁移报告必须记录无法归属的牌组/任务数量。
- 删除不可逆:`downgrade` 第一行 `raise RuntimeError`(fail-closed,延续 V2.3/V2.4 精神);回退仅限恢复升级前备份。
- §0/§1/§2/§3/§6 与 ORM 同批更新(见 structure-contract v2.5)。

### 7.4 迁移工具选型

- 选型:**Alembic**(SQLAlchemy 官方迁移工具);P0-2 引入并生成首个迁移。
- 迁移纪律:与 ORM 模型同 PR 提交;破坏性变更需同步更新 database-design 与契约。

### 7.5 V2.5 多资料落地（V25-D-29~32;revision `b7e4c2a91d50`,不可逆）

项目从"单 PDF"翻面为"资料集合"(契约 3.2a/3.16/6.2);迁移 `downgrade` 抛
`NotImplementedError`(空项目/纯文本项目无法重建 1:1 `file_id` 归属,回退仅限部署备份恢复)。

- **新表**:`materials`(定义见 2.22);回填自 `learning_projects × pdf_files` 既有 1:1 归属,PDF 资料行 `status` 置 NULL。
- **chapters**:加 `material_id NOT NULL FK → materials ON DELETE CASCADE`(回填 = `file_id`)、`material_id` 索引;`file_id` 改可空;`start_page`/`end_page` 改可空(TEXT 章节无页码)。
- **text_chunks**:加 `material_id NOT NULL FK → materials ON DELETE CASCADE`(回填 = `file_id`)与 `chunk_seq NOT NULL`(回填 = `page_number`);`file_id` 改可空;唯一键 `(file_id, page_number)` 与同名索引删除,改 `UNIQUE (material_id, chunk_seq)` + `(material_id, chunk_seq)` 索引。
- **learning_projects**:删除 `file_id` 列(唯一外键权威移交 `materials.project_id`);允许空项目。
- **应用层语义**:项目 `status` 聚合派生含 `EMPTY`;新增/删除任一资料重置 `chapters_confirmed_at`;`tasks.selected_chapters` 快照每项含 `material_id`(新写入保证,历史快照只读保留)。
- §0/§1/§2/§3/§6 与 ORM 同批更新(见 structure-contract v2.5 多资料增量)。
