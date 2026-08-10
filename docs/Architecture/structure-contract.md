# 结构契约 v2.1

前后端接口合同。需求依据:[PRD v2.1](../PRD/V2.1/prd_v2_1.md);机器可读接口定义:[openapi.yaml](openapi.yaml);持久化映射:[database-design.md](database-design.md)。

**字段权威声明**:本章第 3 节资源模型是字段定义的唯一来源;`openapi.yaml` schema 与 `database-design.md` 表结构均从本章派生。

## 1. 总则

### 1.1 数据主体与鉴权(决策 D-02)

- 客户端本地生成 UUID v4 作为**匿名设备 ID**,所有请求携带请求头 `X-Device-ID`。
- 服务端首次见到某设备 ID 时自动建立数据主体,无注册接口。
- 所有资源按设备 ID 隔离;服务端校验资源归属,禁止仅凭资源 ID 访问他人数据。
- 缺失/非法设备 ID → `401 DEVICE_ID_REQUIRED` / `DEVICE_ID_INVALID`。
- **风险声明**:匿名设备 ID 是"等同于密码"的信任凭据,泄漏后(抓包、日志、分享)可被永久冒用,且无找回途径;MVP 缓解:写接口限流(1.6)、风控信号记录(数据库 `devices` 表:首次 IP / UA / 最近活跃)、归属校验统一 404 不暴露存在性。设备 ID 重置与数据迁移接口为后续迭代项。
- **归属声明**:所有资源隐含归属当前 X-Device-ID,持久化列为 `device_id`;无 `device_id` 列的表(chapters、knowledge_points 等)经外键关联路径归属校验。

### 1.2 时间与时区

- 服务端时间为**权威时钟**:到期判断(`due`)与统计分桶一律使用服务端时间。
- 所有时间字段为 ISO 8601 UTC(RFC 3339),例:`2026-08-10T09:00:00Z`。
- 统计接口由客户端上报 `timezone`(IANA 名称,如 `Asia/Shanghai`),服务端按该时区分桶;周起始日为**周一**。
- 复习事件的 `device_timezone` 仅用于看板分桶,不影响排程计算(排程使用 UTC)。

### 1.3 幂等约定

- 所有写操作(创建、追加、删除、任务启动、继续、取消、评级、重写)必须携带请求头 `Idempotency-Key`(客户端生成 UUID v4);**样卡生成(`POST /samples`)无副作用、不落库,豁免幂等键**(对应 6.3)。
- 服务端按 `(device_id, 接口路径含具体资源 ID, idempotency_key)` 去重;**重复请求返回首次成功结果**(2xx,含 204,返回首次响应状态与响应体),不产生重复数据。
- 幂等键相同但请求体与首次不一致 → `409 IDEMPOTENCY_CONFLICT`(客户端应视为编码错误)。
- 评级接口双幂等键优先级:`Idempotency-Key` 幂等表命中优先;未命中时以 `client_event_id` 兜底 —— 同 `client_event_id` 且 `card_id` + `rating` 一致 → 返回首次成功结果;不一致 → `409 REVIEW_EVENT_CONFLICT`。
- 幂等记录 INSERT 与业务副作用必须在**同一事务**内完成(防止响应丢失后同键重试造成双写,破坏 AC-05 重复入库率 0% 与 AC-10 评级去重)。
- 三个专用幂等标识(与 `Idempotency-Key` 并用):
  - `generation_item_id` — 生成卡唯一标识,同一值最多对应一张有效卡片;
  - `client_event_id` — 复习事件幂等标识,设备内唯一;
  - `generation_item_id` 为空的卡片(manual / imported)由 `Idempotency-Key` 保证不重复写入。
- 读操作无需幂等键。

### 1.4 错误响应

统一结构:

```json
{
  "error": {
    "code": "DECK_NOT_FOUND",
    "message": "人类可读的补充信息",
    "localization_key": "error.deck_not_found"
  }
}
```

- 错误码为稳定字符串,客户端按 `localization_key` 映射文案;不随消息文本变化。
- `message` 仅面向用户展示,不得包含堆栈、内部路径、SQL 或他人数据;内部细节进服务端日志(以 `request_id` 关联)。
- 完整错误码表见第 7 章。

### 1.5 API Key 安全(决策 D-03)

- Key 仅经 TLS 上传,服务端**加密保存**,用于发起 DeepSeek 请求;生成任务自动使用已保存 Key。
- Key 不得出现在日志、任务明细、分析数据或任何接口响应中;接口仅返回状态或脱敏标识(`sk-****abcd`)。
- 客户端不得持久化 Key 明文,UI 不展示完整 Key。
- 实现防线:通用请求日志对 `PUT /api-key` 请求体强制掩码;`infra/llm/` 异常统一转换为 `API_KEY_*` / `GENERATION_FAILED` 错误码,日志仅记录 request_id、上游状态码、异常类型,禁止记录异常链与请求对象。

### 1.6 限流策略(匿名体系下的防滥用硬防线)

| 维度 | 默认阈值 | 覆盖接口 |
| --- | --- | --- |
| 写操作 | 60 req/min/device | 全部写接口 |
| IP | 5 req/s/IP | 全部接口 |
| `PUT /api-key` | 10 次/时/device | Key 校验(校验 oracle) |
| `POST /samples` | 20 次/时/device | 样卡生成(消耗模型配额) |
| PDF 上传 | 10 次/时/device | `POST /pdfs` |

超限返回 `429 RATE_LIMITED` + `Retry-After` 响应头;阈值可运维调整,客户端不得硬编码。

### 1.7 传输安全与数据保留

- 全部接口必须经 HTTPS(TLS)访问;生产环境的模型调用与统计接口同样受限。
- PDF 解析失败或生成任务失败时,不删除原始文件(PRD 5.1)。
- 删除 PDF 元数据时同步清理存储对象;存储 key 一律随机 UUID,禁止包含用户输入(filename 等)。

## 2. 术语对照(与 PRD)

| 本契约 | PRD 术语 | 说明 |
| --- | --- | --- |
| `due` | 下次复习时间 | FSRS 字段,到期判断用 |
| `review_state` | 复习状态 | FSRS 排程状态快照 |
| `review_event` | 复习事件 | 评级产生的不可变记录 |
| `AGAIN / HARD / GOOD / EASY` | 忘记 / 模糊 / 记得 /(新增)简单 | FSRS 四档评级 |

## 3. 资源模型(单一事实来源)

命名:JSON 字段统一 snake_case。类型:UUID 为字符串 UUID v4;datetime 为 ISO 8601 UTC。

### 3.1 ApiKey

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | enum | ✓ | `AVAILABLE` / `INVALID` / `INSUFFICIENT_BALANCE` / `UNKNOWN`;未保存 Key 时返回 `UNKNOWN` |
| `masked_key` | string | ✓ | 脱敏标识,如 `sk-****abcd`;未保存时为 `""` |
| `updated_at` | datetime | ✓ | 最近校验时间 |

### 3.2 PdfFile

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file_id` | uuid | ✓ | |
| `filename` | string | ✓ | |
| `size_bytes` | int | ✓ | |
| `status` | enum | ✓ | `PENDING` / `PARSING` / `PARSED` / `FAILED` |
| `error_code` | string | ✗ | 解析失败码(`PDF_PARSE_FAILED` / `PDF_TOC_MISSING`) |
| `chapters` | Chapter[] | ✗ | 解析成功后返回 |
| `created_at` | datetime | ✓ | |

规则:目录解析失败 → `FAILED` + `error_code`,前端终止流程,不提供 AI 猜测兜底(PRD 5.2)。
删除规则:存在非终态任务(`PENDING / RUNNING / PAUSED`)引用该文件时,删除返回 `409 TASK_IN_PROGRESS`;仅终态任务引用或未被引用时可删除(删除后 `tasks.file_id` 置空,任务保留)。

### 3.3 Chapter

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `chapter_id` | uuid | ✓ | |
| `name` | string | ✓ | 可修改 |
| `start_page` | int | ✓ | 可修改 |
| `end_page` | int | ✓ | 可修改 |

### 3.4 Task

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | uuid | ✓ | |
| `file_id` | uuid | 创建时必填 | 删除 PDF 后置 `null`(任务保留) |
| `deck_id` | uuid | 创建时必填 | 删除牌组后置 `null`(任务保留) |
| `status` | enum | ✓ | 见 4.1 |
| `stage` | enum | ✗ | `PLANNING` / `GENERATING`,仅运行期有意义 |
| `selected_chapters` | Chapter[] | ✓ | |
| `generation_config` | GenerationConfig | ✓ | 见 3.5 |
| `cursor` | object | ✗ | `{ "completed_batch_count": int }` 断点续传游标 |
| `generated_card_count` | int | ✓ | 已生成并入库卡片数 |
| `total_batch_count` | int | ✗ | 规划完成后可返回 |
| `completed_batch_count` | int | ✗ | |
| `resumable` | bool | ✓ | 是否可继续(供前端"继续任务"按钮) |
| `failure_stage` | enum | ✗ | `PLANNING` / `GENERATING` / `WRITE_BACK` |
| `error_code` | string | ✗ | 失败码 |
| `created_at` / `started_at` / `ended_at` | datetime | 按需 | |
| `updated_at` | datetime | ✓ | 长任务轮询刷新区分 |

### 3.5 GenerationConfig

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `quantity_tendency` | enum | ✓ | `COMPACT`(精简) / `BALANCED`(均衡) / `EXTENSIVE`(充分覆盖);影响知识点规划粒度:同一章节下 `COMPACT` 规划知识点数 ≤ `BALANCED` ≤ `EXTENSIVE`(可测口径) |
| `difficulty_ratio` | object | ✓ | `{ "basic": 0.4, "understanding": 0.4, "application": 0.2 }`,和为 1 |
| `custom_requirements` | string | ✗ | 仅当前任务生效 |

难度枚举:`BASIC`(基础记忆) / `UNDERSTANDING`(理解分析) / `APPLICATION`(综合应用)。
规则:难度比例与自定义要求均**不继承** —— "继承"语义由客户端本地保存上次配置并在新任务请求中显式携带,服务端不存储默认配置(PRD 5.4.2 / 5.4.3)。

### 3.6 KnowledgePoint

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `knowledge_point_id` | uuid | ✓ | |
| `task_id` | uuid | ✓ | |
| `chapter_id` | uuid | 创建时必填 | 源章节删除后置 `null`(名称可从 `tasks.selected_chapters` 快照还原) |
| `source_chunk_id` | string | ✓ | 来源分片标识 |
| `topic` | string | ✓ | 知识点主题 |
| `priority` | int | ✓ | |
| `status` | enum | ✓ | `PENDING` / `PROCESSED` / `SKIPPED` |

### 3.7 Batch(批次)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `batch_id` | uuid | ✓ | |
| `task_id` | uuid | ✓ | |
| `batch_index` | int | ✓ | 批次序号(即游标) |
| `status` | enum | ✓ | `PENDING` / `PROCESSING` / `SUCCEEDED` / `FAILED` / `SKIPPED` |
| `generated_item_ids` | string[] | ✗ | 本批产出 `generation_item_id` 列表 |
| `retry_count` | int | ✓ | 重试计数(上限 2,共 3 次尝试,见 4.2) |
| `coverage_rate` / `duplicate_rate` | float | ✗ | 整批质量数据(仅观测,FR-10) |
| `difficulty_distribution` / `chapter_distribution` / `card_type_distribution` | object | ✗ | 同上,仅观测 |
| `difficulty_deviation` | float | ✗ | 难度偏差(PRD 5.10) |
| `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens` | int | ✗ | Prompt Cache 记录(FR-11) |
| `request_id` | string | ✗ | 模型请求标识(请求层观测,PRD 6.2) |
| `model` / `prompt_version` / `schema_version` / `rubric_version` | string | ✗ | 版本观测(FR-11:稳定内容结构不变) |
| `duration_ms` | int | ✗ | 请求耗时 |
| `http_status` | int | ✗ | 上游 HTTP 状态 |
| `created_at` / `ended_at` | datetime | 按需 | |

### 3.8 Deck

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `deck_id` | uuid | ✓ | |
| `name` | string | ✓ | |
| `source` | enum | ✓ | `MANUAL` / `IMPORTED` / `GENERATED`(牌组本身的来源;`GENERATED` 为 PDF 制卡新建的归属牌组) |
| `card_count` | int | ✓ | 派生进度(接口计算) |
| `due_count` | int | ✓ | 派生:`due <= now` 的卡片数 |
| `mastered_card_count` | int | ✓ | 派生:掌握判定见 5.3 |
| `review_count` | int | ✓ | 派生:累计复习事件数 |
| `mastery_ratio` | float | ✓ | 派生:`mastered_card_count / card_count`,为 0 时返回 0 |
| `created_at` / `updated_at` / `version` | - | ✓ | `version` 供客户端刷新缓存 |

### 3.9 Card

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `card_id` | uuid | ✓ | |
| `deck_id` | uuid | ✓ | |
| `source` | enum | ✓ | `GENERATED` / `MANUAL` / `IMPORTED` |
| `position` | int | ✓ | 牌组内稳定排序位置,追加时分配 |
| `front` / `back` | string | ✓ | 通用渲染字段(所有卡片) |
| `code` | string | ✗ | 卡片编号 |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE` |
| `question` / `answer` | string | ✗ | 仅 `QUESTION` 卡(决策 D-01) |
| `statement` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `answer_boolean` | bool | ✗ | 仅 `TRUE_FALSE` 卡 |
| `explanation` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `generation_item_id` | string | ✗ | 仅 `GENERATED` 卡;同一值最多对应一张有效卡片 |
| `target_difficulty` | enum | ✗ | 仅 `GENERATED` 卡;生成时目标难度(`BASIC` / `UNDERSTANDING` / `APPLICATION`) |
| `knowledge_point_ids` | uuid[] | ✗ | 仅 `GENERATED` 卡;关联知识点,综合应用卡可关联多个(PRD 5.6) |
| `evidence_score` / `correctness_score` / `difficulty_score` / `learning_value_score` | int | ✗ | Rubric 各维度 0~3,仅 `GENERATED` 卡(PRD 5.9) |
| `rubric_total_score` | int | ✗ | Rubric 总分 0~12,仅 `GENERATED` 卡 |
| `version` | string | ✓ | 变更版本,客户端缓存刷新用(重写时递增,决策 C-05) |
| `created_at` / `updated_at` | datetime | ✓ | |

决策 D-01 说明:判断题保留结构化字段(`statement` / `answer_boolean` / `explanation`),同时后端填充 `front` / `back` 文本供前端通用渲染;判断题专用渲染后续版本补充。手动 / 导入卡 `card_type = QUESTION`,仅用 `front` / `back`。

### 3.10 ReviewState(FSRS 排程状态)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `review_state_id` | uuid | ✓ | |
| `card_id` | uuid | ✓ | 与卡片一对一 |
| `state` | enum | ✓ | `NEW` / `LEARNING` / `REVIEW` / `RELEARNING` |
| `stability` | float | ✓ | FSRS 稳定性(天) |
| `difficulty` | float | ✓ | FSRS 难度(0~10) |
| `due` | datetime | ✓ | 下次到期时间(服务端时间) |
| `last_review` | datetime | ✗ | 上次复习时间 |
| `reps` | int | ✓ | 复习次数 |
| `lapses` | int | ✓ | 遗忘(AGAIN)次数 |
| `last_rating` | enum | ✗ | `AGAIN` / `HARD` / `GOOD` / `EASY` |
| `updated_at` | datetime | ✓ | |

字段与 py-fsrs `Card` 一一对应,由服务端排程器计算,客户端不得自行计算。

### 3.11 ReviewEvent

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `review_event_id` | uuid | ✓ | 服务端生成 |
| `client_event_id` | uuid | ✓ | 客户端生成,设备内唯一,幂等标识 |
| `card_id` | uuid | ✓ | |
| `rating` | enum | ✓ | `AGAIN` / `HARD` / `GOOD` / `EASY` |
| `reviewed_at` | datetime | ✓ | 服务端时间 |
| `device_timezone` | string | ✓ | IANA 时区,仅看板分桶用 |
| `created_at` | datetime | ✓ | |

不可变记录;离线补传或重试(同一 `client_event_id`)不重复计数。

### 3.12 StatsDashboard(数据看板)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `period` | object | ✓ | `{ "start": datetime, "end": datetime, "week_ordinal": int }` 统计周期 |
| `timezone` | string | ✓ | 实际分桶时区 |
| `weekly_activity` | int[7] | ✓ | 周一~周日每日复习事件数 |
| `weekly_total` | int | ✓ | 本周总复习事件数 |
| `week_change_rate` | float \| null | ✓ | `(本周-上周)/上周`;上周为 0 时 null(客户端显示"暂无对比") |
| `weekly_goal` | int \| null | ✓ | 客户端随请求上报(见 6.8);未上报时 null |
| `weekly_goal_progress` | float \| null | ✓ | `min(weekly_total / weekly_goal, 1)`;goal 未上报时 null |
| `updated_at` | datetime | ✓ | 聚合快照生成时间(PRD 6.6:客户端据此判断缓存是否过期) |
| `recall_accuracy` | float \| null | ✓ | 周期内 GOOD 事件 / 全部事件 |
| `first_answer_accuracy` | float \| null | ✓ | 首次评级为 GOOD 的卡数 / 首次复习卡数 |
| `retention_rate` | float \| null | ✓ | 非首次事件中 GOOD 数 / 非首次事件数 |
| `streak_days` | int | ✓ | 截至用户本地当天连续有复习事件的自然日数;统一按本次请求上报的 `timezone` 分桶(不按各事件的 `device_timezone`),避免切换时区口径漂移 |
| `mastered_card_count` | int | ✓ | 掌握卡片数(见 5.3) |
| `has_data` | bool | ✓ | false 时客户端展示空态,不得用固定示例值 |

**分母为 0 的比率一律返回 `null`**,不得以 0% 冒充(PRD 5.16)。

## 4. 状态机

### 4.1 Task

```text
PENDING → RUNNING ⇄ PAUSED → COMPLETED
   │         │  │      │
   │         │  └──────┼──→ CANCELLED(用户取消)
   └─────────┴─────────┴──→ FAILED(系统级不可恢复)
```

- `RUNNING ⇄ PAUSED`:中断 / 继续任务(PRD 4.3、FR-12)。恢复时仅处理未完成批次;`PAUSED → RUNNING` 为原子状态转移(条件更新 `status='PAUSED' AND resumable=1`),并发 resume 失败者返回 `409 TASK_STATE_CONFLICT`。
- **孤儿 RUNNING 恢复**:worker 崩溃可能使任务滞留 `RUNNING`;`RUNNING` 带心跳(每批完成后更新 `updated_at`),心跳超时(30 分钟)后任务视为可恢复,允许 resume 抢占(条件更新 `status='RUNNING' AND updated_at < now-30min`)。
- `FAILED`:仅系统级不可恢复错误(如 API Key 失效、上游持续不可用);保留已入库结果。批次级失败(Schema 重试达上限)不置 `FAILED` —— 该批 `SKIPPED`,任务继续处理其余批次(见 4.2)。
- `CANCELLED`:用户取消,已入库卡片保留。
- 前端页面状态映射(FR-18):`RUNNING` = 生成中,`PAUSED` = 暂停,`COMPLETED` = 完成。

### 4.2 KnowledgePoint / Batch

```text
KnowledgePoint: PENDING → PROCESSED
                      └── SKIPPED

Batch: PENDING → PROCESSING → SUCCEEDED
                        │        └── FAILED → PROCESSING(重试上限 2 次,共 3 次尝试) → 仍失败 → SKIPPED(终态,任务继续)
                        └── SKIPPED
```

已完成批次不得重复执行;已入库卡片(`generation_item_id`)不得重复写入(AC-05)。
批次 `SKIPPED` 不代表任务失败;任务仅在系统级错误(API Key 失效、上游持续不可用)时 `FAILED`(见 4.1)。

### 4.3 卡片复习状态(FSRS)

```text
NEW → LEARNING → REVIEW →(AGAIN)→ RELEARNING →(GOOD/EASY)→ REVIEW
                    └──(AGAIN)→ LEARNING(重学)
```

转移规则由 FSRS-6 算法决定,服务端存储快照即可,客户端无需理解。

## 5. 复习排程(FSRS-6)

### 5.1 引擎与配置

- 引擎:`py-fsrs`(open-spaced-repetition/py-fsrs),FSRS-6。
- 服务端持有统一配置,每次评级调用:

```python
Scheduler(
    parameters=FSRS6_DEFAULT_PARAMETERS,   # 21 参数,py-fsrs 默认权重
    desired_retention=0.9,
    learning_steps=(10m, 1d),              # 已确认 C-01:新卡 10 分钟后复现,次日复现后毕业
    relearning_steps=(10m,),
    maximum_interval=36500,
    enable_fuzzing=False,                  # 已确认 C-02:关闭,保证同输入同输出
).review_card(card, rating)                # → (new_card, review_log)
```

- 响应中新 `due` 即 `review_state.due`;服务端将 `new_card` 全量快照入库。

### 5.2 评级语义

| 评级 | 含义 | 前端文案 |
| --- | --- | --- |
| `AGAIN` | 忘记 | 忘记 |
| `HARD` | 模糊 | 模糊 |
| `GOOD` | 记得 | 记得 |
| `EASY` | 简单 | 简单(本期新增按钮) |

- "开始复习"仅返回 `due <= now` 的卡片,按 `due`、`position` 稳定排序。
- 自由刷题不创建事件、不改变排程;评级接口对到期边界宽容处理(客户端提交任意卡片评级均按 FSRS 计算,决策 C-06)。
- 排程断言样例(fuzzing 关闭后同输入同输出,可据此验收;`due` 为近似值):

| 步骤 | 评级 | 期望状态 | 期望 due |
| --- | --- | --- | --- |
| 新卡首次 | GOOD | LEARNING | ≈ now + 10m |
| 第二次 | GOOD | LEARNING | ≈ now + 1d |
| 第三次 | GOOD | REVIEW | 按 FSRS 计算(> 1d) |
| REVIEW 中 | AGAIN | RELEARNING | ≈ now + 10m |
| RELEARNING 中 | GOOD | REVIEW | 按 FSRS 计算 |

### 5.3 掌握判定

`state == REVIEW` 且 `stability >= 21` 天(已确认 C-03;与 Anki 成熟间隔口径一致)。

用于:`mastered_card_count`、`mastery_ratio`、看板"已掌握卡片"。仅统计口径,不影响排程。

## 6. 接口清单(人读版)

机器可读权威见 `openapi.yaml`。除特别注明外,写操作均需 `Idempotency-Key`。

### 6.1 PDF(FR-01/FR-02)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/pdfs` | 上传 PDF(multipart),返回 PdfFile,异步解析 | ✓ |
| GET | `/v1/pdfs` | 最近使用的 PDF 列表 | - |
| GET | `/v1/pdfs/{file_id}` | 详情 + 章节列表;`PARSING` 时轮询 | - |
| PATCH | `/v1/pdfs/{file_id}/chapters/{chapter_id}` | 修改章节名称 / 起始页 / 结束页(PRD 4.1 第 3 步、AC-02;审核修复) | ✓ |
| DELETE | `/v1/pdfs/{file_id}` | 删除文件元数据;存在非终态任务引用时返回 `409 TASK_IN_PROGRESS` | ✓ |

上传限制:≤ 50MB、≤ 500 页;校验:文件魔数 + 扩展名 + MIME 三重检查,不合规 → `400 PDF_UPLOAD_INVALID`(审核修复,防解析 DoS)。

### 6.2 API Key(FR-17)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| PUT | `/v1/api-key` | 验证并保存,返回 ApiKey(仅状态与脱敏标识) | ✓ |
| GET | `/v1/api-key/status` | 查询状态;未保存时返回 `200` + `status=UNKNOWN`、`masked_key=""` | - |

校验结果一律经 `200 + ApiKey.status` 返回(`INVALID` / `INSUFFICIENT_BALANCE` 属正常业务结果,不产生 422 错误响应);`INVALID` 校验结果**不覆盖**已存在的有效 Key(防冒用者替换他人有效 Key),`AVAILABLE`(更换 Key)才覆盖。

### 6.3 样卡(FR-05)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/samples` | 生成 3 张样卡(1 基础 + 1 理解 + 1 应用;2 问答 + 1 判断);重新生成即再次调用 | - |

请求体:`{ file_id, chapter_ids[], generation_config }`。样卡不入库、不参与统计(响应直接返回卡片结构)。

### 6.4 任务(FR-06/07/12)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/tasks` | 接受样卡 → 创建任务并启动生成(含规划与分批) | ✓ |
| GET | `/v1/tasks/{task_id}` | 长任务轮询:状态、stage、已生成数、批次进度、失败码、是否可继续(FR-18) | - |
| POST | `/v1/tasks/{task_id}/resume` | 断点续传,仅处理未完成批次 | ✓ |
| POST | `/v1/tasks/{task_id}/cancel` | 取消任务 | ✓ |

### 6.5 牌组与卡片(FR-03/14)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/decks` | 牌组列表(含进度摘要) | - |
| POST | `/v1/decks` | 新建牌组 `{ name }` | ✓ |
| GET | `/v1/decks/{deck_id}` | 详情 + 进度(card_count / due_count / mastered / review_count / mastery_ratio) | - |
| DELETE | `/v1/decks/{deck_id}` | 删除牌组及其卡片、复习状态与统计;重复提交安全返回;存在非终态任务引用时返回 `409 TASK_IN_PROGRESS` | ✓ |

删除后 `tasks.deck_id` 置空,历史任务保留。
列表接口 MVP 暂不分页:`GET /v1/decks` 与 `GET /v1/decks/{deck_id}/cards` 返回全量;单牌组卡片量超 1000 张后再引入分页。
| POST | `/v1/decks/{deck_id}/cards` | 手动新增卡片 `{ front, back }`,分配 position | ✓ |
| POST | `/v1/decks/{deck_id}/cards/import` | 批量导入 `{ cards: [{ front, back }] }`,原子写入,返回逐张结果 | ✓ |

导入规则:客户端负责文本解析与预览编辑;服务端仅接收最终确认列表;无法识别的行由客户端在预览阶段拦截(PRD 5.14)。

### 6.6 复习(FR-15)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/decks/{deck_id}/review` | 到期卡片队列(due <= now,按 due、position 排序) | - |
| GET | `/v1/decks/{deck_id}/cards` | 自由刷题:全部卡片(不创建事件) | - |
| POST | `/v1/review-events` | 提交评级 `{ card_id, rating, client_event_id, device_timezone }`,返回更新后的 ReviewState | ✓(client_event_id) |

### 6.7 单卡重写(FR-13,决策 D-04)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/cards/{card_id}/rewrite` | 重写:生成新版本 → Schema 校验 → Rubric 记录 → 原地替换原卡 | ✓ |

规则(已确认 C-05):

- **原地替换,保持同一 `card_id`**:前端按"内容更新"刷新该卡,position / 列表位置不变。
- 新版本分配**新的 `generation_item_id`**,旧标识作废(契约"同一 generation_item_id 最多对应一张有效卡片"仍成立)。
- `review_state` **重置为新卡初始状态**,重新进入学习排程(内容已变,旧记忆不适用)。
- `updated_at` / `version` 递增;重写失败保留原卡及原有排程状态不变。
- Rubric 不影响替换结果(AC-06)。
- 响应与任务明细**不暴露 PDF 来源信息**(来源仅 Rubric 内部使用,PRD 5.13)。

### 6.8 数据看板(FR-16)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/stats/dashboard?timezone=Asia/Shanghai&weekly_goal=50` | 当前自然周看板,返回 StatsDashboard;`weekly_goal` 为可选参数(客户端本地保存的目标,未传时 `weekly_goal` / `weekly_goal_progress` 返回 null) | - |

## 6.9 质量观测(FR-10/11,AC-07;审核修复)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/tasks/{task_id}/batches` | 返回批次列表(Batch),含单卡 Rubric 汇总、质量分布与 Prompt Cache 记录,供测试与联调核验(AC-07) | - |

MVP 无可视化后台;观测数据经此接口 + 卡片详情(Rubric 单卡字段,3.9)核验。

## 7. 错误码表

| 分组 | 错误码 | HTTP | 说明 |
| --- | --- | --- | --- |
| 通用 | `VALIDATION_ERROR` | 400 | 请求结构/字段非法(含 `device_timezone` 非 IANA 时区) |
| | `RATE_LIMITED` | 429 | 限流(1.6),响应携带 `Retry-After` |
| | `IDEMPOTENCY_CONFLICT` | 409 | 幂等键相同但请求体与首次不一致 |
| | `INTERNAL_ERROR` | 500 | 未预期错误(内部细节仅进日志) |
| 设备 | `DEVICE_ID_REQUIRED` | 401 | 缺 X-Device-ID |
| | `DEVICE_ID_INVALID` | 401 | 设备 ID 格式非法 |
| PDF | `PDF_UPLOAD_INVALID` | 400 | 非 PDF / 损坏 / 超限(50MB / 500 页) |
| | `PDF_PARSE_FAILED` | 422 | 文本层解析失败 |
| | `PDF_TOC_MISSING` | 422 | 无可用目录结构(终止流程) |
| | `PDF_NOT_FOUND` | 404 | 不存在或非本设备(统一 404,不暴露存在性) |
| API Key | `API_KEY_UNAVAILABLE` | 502 | 上游校验不可用,可重试 |
| | `API_KEY_NOT_SET` | 422 | 样卡 / 任务启动时未保存 Key |
| 任务 | `TASK_NOT_FOUND` | 404 | |
| | `TASK_STATE_CONFLICT` | 409 | 非法状态转移(并发 resume、重复完成) |
| | `TASK_NOT_RESUMABLE` | 409 | 任务不可继续 |
| | `TASK_IN_PROGRESS` | 409 | 删除保护:资源被非终态任务引用(PDF / 牌组) |
| | `GENERATION_FAILED` | 500 | 系统级生成失败(任务 FAILED);批次级失败不产生错误响应 |
| 牌组/卡片 | `DECK_NOT_FOUND` | 404 | 不存在或非本设备(统一 404,不暴露存在性) |
| | `CARD_NOT_FOUND` | 404 | |
| | `GENERATION_ITEM_CONFLICT` | 409 | `generation_item_id` 已对应其他卡 |
| | `IMPORT_PARSE_ERROR` | 422 | 导入内容非法(逐行错误随响应返回;客户端预览阶段已拦截为主) |
| 复习 | `REVIEW_EVENT_INVALID` | 400 | 评级非法 |
| | `REVIEW_EVENT_CONFLICT` | 409 | 同 `client_event_id` 但 `card_id` / `rating` 与首次不一致 |

注:API Key 校验结果(`INVALID` / `INSUFFICIENT_BALANCE`)经 `200 + ApiKey.status` 返回,不产生错误响应(见 6.2)。跨设备资源访问一律返回 404,不暴露资源存在性(1.1)。

## 8. 与 PRD 的对照

| 契约章节 | PRD 章节 | 状态 |
| --- | --- | --- |
| 1.1 数据主体 | 3.2 / 7.1 服务端访问控制、D-02 | 一致 |
| 1.5 API Key | 5.17 / 7.1 / AC-11、D-03 | 一致(PRD 已同步修订) |
| 3.9 判断题结构 | 5.8 / D-01 | 一致 |
| 5 复习排程 | 5.15 / 6.6 / AC-10 | 一致(PRD 已同步修订为 FSRS) |
| 6.7 单卡重写 | 5.13 / AC-06、D-04 | 一致 |
| 5.3 掌握判定 | 5.15 牌组进度 / 5.16 / 6.5 | 一致(PRD 已同步修订) |
| 1.6 限流 / 1.7 传输与保留 | 7.1 数据安全 / 5.1 | 新增(审核修复) |
| 6.1 章节修改与上传限制 | 4.1 第 3 步 / FR-02 / AC-02 | 新增(审核修复) |
| 6.9 质量观测 | FR-10 / FR-11 / AC-07 | 新增(审核修复) |
| 3.9 Rubric 与关联字段 | 5.9 / 5.6 / 6.3 / AC-07 | 修复(审核) |
