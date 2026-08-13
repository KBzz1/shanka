# 结构契约 v2.2

前后端接口合同。需求依据:[PRD v2.2](../PRD/V2.2/prd_v2_2.md)(继承 [v2.1](../PRD/V2.1/prd_v2_1.md));机器可读接口定义:[openapi.yaml](openapi.yaml);持久化映射:[database-design.md](database-design.md)。

**字段权威声明**:本章第 3 节资源模型是字段定义的唯一来源;`openapi.yaml` schema 与 `database-design.md` 表结构均从本章派生。

## 1. 总则

### 1.1 数据主体与鉴权(决策 D-05)

- 用户经用户名/密码**注册或登录**获得 opaque Bearer session token;受保护请求携带 `Authorization: Bearer <token>`(FR-19)。
- 注册/登录接口(6.11)无鉴权;探针与匿名系统端点(8.2/8.3)豁免 Bearer;其余业务接口全部需要 Bearer。
- 所有资源按 `user_id` 隔离;服务端校验资源归属,禁止仅凭资源 ID 访问他人数据;跨用户访问统一 404,不暴露存在性。
- 缺失/非法/撤销/过期 token → `401 AUTH_REQUIRED` / `AUTH_INVALID`,一律携带 `WWW-Authenticate: Bearer` 响应头。
- **凭据规则**:用户名 3~32 位、仅 `[a-z0-9._-]`、服务端统一转小写;密码 8~128 字符、不截断、不做 normalization;密码 Argon2id(≥ memory_cost=19456 KiB / time_cost=2 / parallelism=1);登录失败统一 `401 INVALID_CREDENTIALS`(用户名不存在时做固定 dummy 校验),用户名冲突 `409 USERNAME_TAKEN`。
- **会话规则**:256-bit 随机 opaque token,数据库只存 SHA-256 摘要;默认 30 天绝对有效期、无滑动续期、无 refresh;logout 只撤销当前会话;同一用户允许多会话。
- **风险声明**:session token 等同于密码,泄漏后在被撤销或过期前可被冒用;缓解:注册/登录按 IP 与用户名限流(1.6)、token 只存摘要、敏感信息不落日志(1.5/8.1)。
- **归属声明**:所有资源隐含归属当前登录 `user_id`,持久化列为 `user_id`;无 `user_id` 列的表(chapters、knowledge_points 等)经外键关联路径归属校验。v2.1 遗留的 `device_id` 数据不迁移、不认领、无访问路径(决策 D-06);`devices` 与旧 `device_id` 列仅兼容审计,不参与认证/授权。

### 1.2 时间与时区

- 服务端时间为**权威时钟**:到期判断(`due`)与统计分桶一律使用服务端时间。
- 所有时间字段为 ISO 8601 UTC(RFC 3339),例:`2026-08-10T09:00:00Z`。
- 统计接口由客户端上报 `timezone`(IANA 名称,如 `Asia/Shanghai`),服务端按该时区分桶;周起始日为**周一**。
- 复习事件的 `device_timezone` 仅用于看板分桶,不影响排程计算(排程使用 UTC)。

### 1.3 幂等约定

- 所有写操作(创建、追加、删除、任务启动、继续、取消、评级、重写)必须携带请求头 `Idempotency-Key`(客户端生成 UUID v4);**样卡生成(`POST /samples`)无副作用、不落库,豁免幂等键**(对应 6.3)。
- 服务端按 `(user_id, 接口路径含具体资源 ID, idempotency_key)` 去重;**重复请求返回首次成功结果**(2xx,含 204,返回首次响应状态与响应体),不产生重复数据。
- 幂等键相同但请求体与首次不一致 → `409 IDEMPOTENCY_CONFLICT`(客户端应视为编码错误)。
- 评级接口双幂等键优先级:`Idempotency-Key` 幂等表命中优先;未命中时以 `client_event_id` 兜底 —— 同 `client_event_id` 且 `card_id` + `rating` 一致 → 返回首次成功结果;不一致 → `409 REVIEW_EVENT_CONFLICT`。
- 幂等记录 INSERT 与业务副作用必须在**同一事务**内完成(防止响应丢失后同键重试造成双写,破坏 AC-05 重复入库率 0% 与 AC-10 评级去重)。
- 三个专用幂等标识(与 `Idempotency-Key` 并用):
  - `generation_item_id` — 生成卡唯一标识,同一值最多对应一张有效卡片;
  - `client_event_id` — 复习事件幂等标识,用户内唯一;
  - `generation_item_id` 为空的卡片(manual / imported)由 `Idempotency-Key` 保证不重复写入。
- 读操作无需幂等键。
- logout 顺序重放语义:撤销成功后同键重试将先被认证闸门 401(AUTH_INVALID)拦截(幂等重放不可达),
  幂等键的实际作用是并发双发单副作用;终态一致(会话确已撤销)。

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
- 受保护接口的 401(`AUTH_REQUIRED` / `AUTH_INVALID`)携带 `WWW-Authenticate: Bearer` 响应头(1.1);登录失败的 `INVALID_CREDENTIALS` 不要求该头。
- `message` 仅面向用户展示,不得包含堆栈、内部路径、SQL 或他人数据;内部细节进服务端日志(以 `request_id` 关联)。
- **生成链路重试分类**:适配层向上区分可重试性(错误码与 HTTP 见第 7 章)——chat 401(Key 错误)不可重试 → 任务 `FAILED` 不重试;429/5xx/网络/超时可重试(429/5xx 记 `API_KEY_UNAVAILABLE`,网络/超时与响应解析失败内部记 `GENERATION_FAILED`)→ 账本预算内重试(每操作 2 次重试 = 3 次尝试);输出非法走业务重试,预算同上。
- 完整错误码表见第 7 章。

### 1.5 API Key 安全(决策 D-03)

- Key 仅经 TLS 上传,服务端**加密保存**,用于发起 DeepSeek 请求;生成任务自动使用已保存 Key。
- Key 不得出现在日志、任务明细、分析数据或任何接口响应中;接口仅返回状态或脱敏标识(`sk-****abcd`)。
- 客户端不得持久化 Key 明文,UI 不展示完整 Key。
- 实现防线:通用请求日志对 `PUT /api-key` 请求体强制掩码;`infra/llm/` 异常统一转换为 `API_KEY_*` / `GENERATION_FAILED` 错误码,日志仅记录 request_id、上游状态码、异常类型,禁止记录异常链与请求对象。

### 1.6 限流策略(账号体系下的防滥用硬防线)

| 维度 | 默认阈值 | 覆盖接口 |
| --- | --- | --- |
| 写操作 | 60 req/min/user | 全部写接口 |
| IP | 5 req/s/IP | 全部接口 |
| 注册/登录 | 按来源 IP(默认阈值运维可调) | `POST /auth/register`、`POST /auth/login` |
| 登录(用户名分桶) | 按规范化用户名(默认阈值运维可调) | `POST /auth/login`(防单账号分布式猜测) |
| `PUT /api-key` | 10 次/时/user | Key 校验(校验 oracle) |
| `POST /samples` | 20 次/时/user | 样卡生成(消耗模型配额) |
| PDF 上传 | 10 次/时/user | `POST /pdfs` |

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
| `generation_unit`(生成单元) | 知识点 | 最小规划单元:一个锚定卡片类型与目标难度的生成任务(见 3.6);数据库表名保持 `knowledge_points`(兼容壳) |

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
| `stage` | enum | ✗ | `PLANNING` / `GENERATING` / `SCORING`,仅运行期有意义 |
| `selected_chapters` | Chapter[] | ✓ | 两阶段语义:PENDING 时为创建快照(保留用户请求与任务视图);首次 PLANNING 抢占时按快照 `chapter_id` 重读章节最新 `name/start_page/end_page` 原子覆盖并冻结为规划快照(见 4.1) |
| `generation_config` | GenerationConfig | ✓ | 见 3.5 |
| `cursor` | object | ✗ | `{ "completed_batch_count": int }` 断点续传游标 |
| `generated_card_count` | int | ✓ | 已生成并入库卡片数 |
| `total_batch_count` | int | ✗ | 规划完成后可返回 |
| `completed_batch_count` | int | ✗ | |
| `completion_reason` | string | ✗ | `NO_GENERATION_UNITS`:全组规划成功但 0 个合法单元的业务空结果(COMPLETED,见 4.1 空单元三分支) |
| `skipped_planning_group_count` | int | ✓ | 部分规划组失败被跳过的组数(仅部分成功时 > 0) |
| `resumable` | bool | ✓ | 是否可继续(供前端"继续任务"按钮) |
| `failure_stage` | enum | ✗ | `PLANNING` / `GENERATING` / `SCORING` / `WRITE_BACK` |
| `error_code` | string | ✗ | 失败码 |
| `created_at` / `started_at` / `ended_at` | datetime | 按需 | |
| `updated_at` | datetime | ✓ | 长任务轮询刷新区分 |

### 3.5 GenerationConfig

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `quantity_tendency` | enum | ✓ | `COMPACT`(精简) / `BALANCED`(均衡) / `EXTENSIVE`(充分覆盖);密度只控制**单元预算(上限)**:任务总预算 = 章节数 × 每章基础单元预算 3 × 密度系数(`COMPACT`=1 / `BALANCED`=2 / `EXTENSIVE`=3),即 2 章 6/12/18。可测口径:只承诺预算上限满足 `COMPACT` < `BALANCED` < `EXTENSIVE`;Planner 允许因内容不足少产出,不同任务的实际单元数不承诺严格单调(PRD 5.4.1) |
| `difficulty_ratio` | object | ✓ | `{ "basic": 0.4, "understanding": 0.4, "application": 0.2 }`,和为 1;三层配额(任务总配额 → 章配额 → 子配额)由代码以最大余数法确定性计算,固定顺序(BASIC < UNDERSTANDING < APPLICATION、章序、组序)消除随机性;Planner 每次调用只拿本组子配额,对某难度超配额时按输出数组相对顺序确定性截断,不重试 |
| `custom_requirements` | string | ✗ | 仅当前任务生效 |

难度枚举:`BASIC`(基础记忆) / `UNDERSTANDING`(理解分析) / `APPLICATION`(综合应用);综合应用单元产出开放性问题(场景化提问)或场景判断题,不要求聚合多个原子知识点(组合规则见 3.6)。
规则:难度比例与自定义要求均**不继承** —— "继承"语义由客户端本地保存上次配置并在新任务请求中显式携带,服务端不存储默认配置(PRD 5.4.2 / 5.4.3)。

### 3.6 KnowledgePoint(生成单元)

"知识点"概念改名**生成单元(GenerationUnit)**:最小规划单元 = 一个锚定卡片类型与目标难度的生成任务。数据库表名保持 `knowledge_points`(兼容壳),ORM 注释与契约称谓按生成单元更新。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `knowledge_point_id` | uuid | ✓ | 单元 ID(服务端生成) |
| `task_id` | uuid | ✓ | |
| `chapter_id` | uuid | 创建时必填 | 源章节删除后置 `null`(名称可从 `tasks.selected_chapters` 快照还原) |
| `source_chunk_ids` | string[] | ✓ | 来源页文本标识列表(`text_chunks.chunk_id`,一页一个);LLM 只能引用本次调用实际提供的页,不能编造(运行时取原文以此列为权威) |
| `source_chunk_id` | string | ✓ | 兼容投影列 = `source_chunk_ids[0]`(新单元写入;旧数据继续按此列读取) |
| `learning_objective` | string | ✓ | 学习目标(Planner 输出,语义复用 `topic` 列;不再用"第X章-知识点N"占位) |
| `target_difficulty` | enum | ✓ | `BASIC` / `UNDERSTANDING` / `APPLICATION`(规划锚定,旧数据为 null) |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE`(规划锚定,旧数据为 null) |
| `priority` | int | ✓ | 全局顺序(服务端按章序、组序、组内数组顺序合并分配,Planner 不输出数值) |
| `status` | enum | ✓ | `PENDING` / `PROCESSED` / `SKIPPED` |

**双维锚定**:卡型与目标难度是两个独立维度,规划时同时锚定、生成时遵循,互不派生。Generator 输出卡型不符合锚定 → 代码校验拒绝(进入该单元重试预算);`Card.target_difficulty` 由服务端写规划锚定值(生成时落库,评分时不再补写),不要求模型回传;内容是否达到目标难度由 Rubric 观测。

**组合规则**(难度 × 卡型,规划锚定的形态约束):

| 难度 | 卡型 | 形态 | 约束 |
| --- | --- | --- | --- |
| BASIC | QUESTION(默认) / TRUE_FALSE | 原子事实/定义直问或二值判断 | 判断题表述无歧义 |
| UNDERSTANDING | QUESTION / TRUE_FALSE | 对比/推理/因果 | 同上 |
| APPLICATION | QUESTION(默认) | 开放性问题(场景化提问,答案多角度分析) | 默认形态 |
| APPLICATION | TRUE_FALSE(允许) | **场景判断题** | ① 需应用规则/概念才能判断(不能是事实换皮);② 结论可明确二值化;③ `explanation` 给出判断依据 |

校验层不做语义拦截(无法可靠判断"是否事实换皮")——由 Prompt 约束 + Rubric 观测。事实换皮但内容正确、有据时主要降低 `difficulty_score` / `learning_value_score`;只有场景加入无来源条件时才降低 `evidence_score`,结论错误时才降低 `correctness_score`,不得混淆四维含义。

**每单元 1 卡**(N=1 固定):Generator 输出恰好 1 张锚定类型卡,乘法放大与批部分成功歧义同步消除(批语义见 3.7)。

### 3.7 Batch(批次)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `batch_id` | uuid | ✓ | |
| `task_id` | uuid | ✓ | |
| `generation_unit_id` | uuid | ✓ | 生成单元外键(`knowledge_points.knowledge_point_id`);**批 = 单元**:每单元 1 批、1 次生成调用,`UNIQUE(task_id, generation_unit_id)`;旧批次迁移列为 null(仅兼容) |
| `batch_index` | int | ✓ | 批次序号(即游标)= 单元序号(1..N) |
| `status` | enum | ✓ | `PENDING` / `PROCESSING` / `SUCCEEDED` / `FAILED` / `SKIPPED` |
| `generated_item_ids` | string[] | ✗ | 本批产出 `generation_item_id` 列表(每单元 1 卡,成功时为单值) |
| `retry_count` | int | ✓ | 重试计数(生成阶段兼容投影;尝试数与重试预算以 `llm_call_attempts` 账本为权威,见 4.2/8.5) |
| `coverage_rate` / `duplicate_rate` | float | ✗ | 质量观测(FR-10);`coverage_rate` = 该单元是否产出合法卡(0/1,不再恒定 1.0),SKIPPED 批次 = 0 |
| `difficulty_distribution` / `chapter_distribution` / `card_type_distribution` | object | ✗ | 同上,仅观测;批=单元后为单值分布 |
| `difficulty_deviation` | float | ✗ | 难度偏差(PRD 5.10) |
| `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens` | int | ✗ | Prompt Cache 记录(FR-11);**生成阶段兼容投影**,全阶段 token 权威在 `llm_call_attempts` |
| `request_id` | string | ✗ | 模型请求标识(请求层观测,PRD 6.2) |
| `model` / `prompt_version` / `schema_version` / `rubric_version` | string | ✗ | 版本观测(FR-11):生成调用实际使用 generator-output schema 的输出版本,Card v1 是服务端投影后的第二层校验;各调用实际使用的具体 asset name+version 在 `llm_call_attempts` 逐调用记录 |
| `duration_ms` | int | ✗ | 请求耗时 |
| `http_status` | int | ✗ | 上游 HTTP 状态 |
| `created_at` / `ended_at` | datetime | 按需 | |

规则:SUCCEEDED = 恰好 1 张合法卡;合法显式空数组(`{"cards":[]}`)= `SOURCE_INSUFFICIENT` 直接 SKIPPED,不做相同输入的无意义重试;非法响应或锚定不符才进入重试预算,耗尽后 SKIPPED。Batch 的重试/token/版本投影必须由同一次调用结果同步写入,不得形成第二套重试预算。

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
| `target_difficulty` | enum | ✗ | 仅 `GENERATED` 卡;规划锚定的目标难度,生成入库时由服务端写入(评分时不再补写;3.6 双维锚定) |
| `knowledge_point_ids` | uuid[] | ✗ | 仅 `GENERATED` 卡;关联生成单元(兼容列;本工作包起生成路径不再写多知识点聚合,综合应用语义见 3.6 组合规则) |
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
| `difficulty` | float | ✓ | FSRS 难度(1~10,py-fsrs 口径) |
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
| `client_event_id` | uuid | ✓ | 客户端生成,用户内唯一,幂等标识 |
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

### 3.13 SampleCard

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `card_id` | uuid | ✓ | 样卡预览标识（不入库,不保证跨请求稳定） |
| `front` / `back` | string | ✓ | 通用渲染字段(所有卡片) |
| `code` | string | ✗ | 卡片编号 |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE` |
| `question` / `answer` | string | ✗ | 仅 `QUESTION` 卡 |
| `statement` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `answer_boolean` | bool | ✗ | 仅 `TRUE_FALSE` 卡 |
| `explanation` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `target_difficulty` | enum | ✗ | `BASIC` / `UNDERSTANDING` / `APPLICATION` |

与 Card 的差异：删去落库/归属/版本语义字段（deck_id、position、source、
generation_item_id、knowledge_point_ids、Rubric 四维与总分、version、created_at、
updated_at）——样卡不入库、不参与统计与 Rubric（PRD 5.5 数据规则），仅承载
前端预览所需结构。

### 3.14 AuthUser(账号,V2.2 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | uuid | ✓ | 数据主体标识;全部业务资源的归属键(1.1) |
| `username` | string | ✓ | 3~32 位,`[a-z0-9._-]`,服务端统一转小写,全库唯一 |
| `created_at` | datetime | ✓ | |

规则:不返回密码/hash;username 唯一性以转小写后的规范化值为准(决策 D-05)。

### 3.15 AuthSessionResponse(会话,V2.2 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user` | AuthUser | ✓ | 最小用户资料 |
| `access_token` | string | ✓ | 256-bit 随机 opaque token;仅本响应与客户端安全存储出现,服务端只存 SHA-256 摘要 |
| `token_type` | string | ✓ | 恒为 `Bearer` |
| `expires_at` | datetime | ✓ | 会话绝对过期时间(默认签发 + 30 天) |

规则:register(201)与 login(200)共用本形状;logout 撤销当前会话返回 204;`GET /auth/me` 只返回 `user`。
客户端不得自动重试 register/login(防网络重放静默创建多条会话,FR-19)。

## 4. 状态机

### 4.1 Task

```text
POST /tasks → PENDING(stage=PLANNING,创建即返回,幂等保持)
                 │ 规划 worker CAS 抢占(并发单执行者,不需要用户 resume)
                 ▼
RUNNING: stage PLANNING ──规划完成──→ GENERATING ──批循环完成──→ SCORING ──评分完成──→ COMPLETED
   │                    ⇄ PAUSED            │                      │
   │                                        └──────────────────────┼──→ CANCELLED(用户取消)
   └───────────────────────────────────────────────────────────────┴──→ FAILED(系统级不可恢复)
```

- **PENDING 创建语义**:`POST /tasks` 校验章节归属后写入创建时 `selected_chapters` 快照(保留用户请求与 PENDING 视图)即返回,`status=PENDING + stage=PLANNING`;任务预算超全局硬上限时创建前直接 `400 VALIDATION_ERROR`,不创建任务、不调用 Planner。
- **PLANNING 阶段**:规划 worker 条件更新 CAS1(`status='PENDING' AND stage='PLANNING'` → `RUNNING`)抢占,同一短事务内按快照 `chapter_id` 重读章节最新 `name/start_page/end_page` 覆盖 `selected_chapters` 并冻结为**规划快照**(抢占前发生的章节页码修改进入本任务,提交后不再影响;孤儿恢复不得再次刷新);所选章节已删除或已不属于该 PDF → 任务 `FAILED`(`failure_stage=PLANNING`,内部原因区分 `CHAPTER_SNAPSHOT_STALE`)。孤儿 `RUNNING+PLANNING` 心跳超时(30 分钟)经 CAS2 接管,接管后先把遗留 STARTED 调用置 UNKNOWN 再按账本恢复。
- **GENERATING 阶段**:生成 worker 扫描 `RUNNING + stage=GENERATING`(加 stage 条件,避免与规划中任务冲突);批次级抢占沿用 V5B 条件更新;批循环全部完成 → 条件更新 `stage=GENERATING → SCORING`。
- **SCORING 阶段**:`RUNNING + stage=SCORING`,心跳/孤儿沿用;单次评分 LLM 失败不重试、不阻塞(账本记 FAILED/UNKNOWN,卡片保留);评分完成条件更新 `RUNNING + SCORING → COMPLETED`。`failure_stage=SCORING` 仅用于评分 worker 自身的不可恢复基础设施/数据库错误。进入 SCORING 时卡片已可读(前端显示"卡片已生成,质量统计处理中")。
- **空单元三分支**(规划结果,决策拍板):全组成功但合法单元 0 个 → `COMPLETED`(`total/completed_batch_count=0`、`generated_card_count=0`、`completion_reason=NO_GENERATION_UNITS`,业务合法结果);全部规划组因上游/输出错误失败 → `FAILED + failure_stage=PLANNING`(不伪装成业务空结果);部分组失败 → 成功组继续生成,`skipped_planning_group_count` 记录跳过组数。
- `RUNNING ⇄ PAUSED`:**PAUSED 为预留状态**——PRD 5.12 仅定义任务恢复，当前无 `pause` 端点与实现写入路径；前端「暂停生成」按钮为本地展示状态，不改变服务端 RUNNING。未来补暂停功能时按本状态机实现（`PAUSED → RUNNING` 原子转移 + `resumable` 抢占）。
- 恢复(resume)时仅处理未完成批次;并发 resume 失败者返回 `409 TASK_STATE_CONFLICT`。
- **孤儿 RUNNING 恢复**:worker 崩溃可能使任务滞留 `RUNNING`;`RUNNING` 带心跳(每批完成后更新 `updated_at`),心跳超时(30 分钟)后任务视为可恢复,允许 resume 抢占(条件更新 `status='RUNNING' AND updated_at < now-30min`)。
- `FAILED`:仅系统级不可恢复错误(如 API Key 失效、上游持续不可用);保留已入库结果。批次级失败(Schema 重试达上限)不置 `FAILED` —— 该批 `SKIPPED`,任务继续处理其余批次(见 4.2)。
- `CANCELLED`:用户取消,已入库卡片保留;cancel 覆盖 PENDING/RUNNING(含 PLANNING/GENERATING/SCORING),评分阶段取消停止后续评分但保留全部已生成卡。
- 前端页面状态映射(FR-18):`RUNNING` = 生成中,`PAUSED` = 暂停,`COMPLETED` = 完成。

### 4.2 KnowledgePoint / Batch

```text
KnowledgePoint(生成单元): PENDING → PROCESSED
                              └── SKIPPED

Batch: PENDING → PROCESSING → SUCCEEDED
                        │        └── FAILED → PROCESSING(重试上限 2 次,共 3 次尝试) → 仍失败 → SKIPPED(终态,任务继续)
                        └── SKIPPED(含 SOURCE_INSUFFICIENT 合法弃权,不重试)
```

已完成批次不得重复执行;已入库卡片(`generation_item_id`)不得重复写入(AC-05)。
批次 `SKIPPED` 不代表任务失败;任务仅在系统级错误(API Key 失效、上游持续不可用)时 `FAILED`(见 4.1)。
重试预算与尝试计数以 `llm_call_attempts` 账本为权威:同一 operation_key 的全部 STARTED/SUCCESS/FAILED/UNKNOWN 尝试计入预算(孤儿 STARTED 转 UNKNOWN 仍计数),达到预算不再发请求;调用前必须先有已提交的 STARTED 占位行。

### 4.3 卡片复习状态(FSRS)

```text
NEW → LEARNING → REVIEW →(AGAIN)→ RELEARNING →(GOOD/EASY)→ REVIEW
                    └──(AGAIN)→ LEARNING(重学)
```

转移规则由 FSRS-6 算法决定,服务端存储快照即可,客户端无需理解。

### 4.4 任务执行架构定式

- **进程内调度器**：PDF 解析、规划 worker、生成 worker、评分 worker 由 API 进程内后台循环扫描执行；规划/评分 worker 以条件更新 CAS 抢占（并发单执行者），任务/批次状态与游标存 DB（**DB 即状态**），LLM 调用尝试与全阶段 token 以 `llm_call_attempts` 账本为权威，不引入外部任务队列（Celery/RQ/Redis）。
- **多实例演进**：孤儿 RUNNING 心跳恢复（30 分钟）+ DB 条件更新抢占已支持多 worker；未来多实例仅增加 DB 轮询调度，业务逻辑不变。
- 禁止以性能为由提前引入任务队列。

## 5. 复习排程(FSRS-6)

### 5.1 引擎与配置

- 引擎:`py-fsrs`(open-spaced-repetition/py-fsrs),FSRS-6。
- 服务端持有统一配置,每次评级调用:

```python
Scheduler(
    parameters=FSRS6_DEFAULT_PARAMETERS,   # 19 参数,py-fsrs 4.x 默认权重(R-13:固定 4.x 线,6.x 为 21 参数)
    desired_retention=0.9,
    learning_steps=(10m, 10m, 1d),         # R-13 裁决:py-fsrs 语义下 GOOD 间隔=steps[step+1],3 步配置复现 5.2 表并符合 C-01 意图(新卡 10 分钟后复现,次日复现后毕业)
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
| DELETE | `/v1/pdfs/{file_id}/chapters/{chapter_id}` | 删除章节(关联 `knowledge_points.chapter_id` 置 null,3.6;历史任务 `selected_chapters` 快照不受影响;仅 PARSED 后可删) | ✓ |
| DELETE | `/v1/pdfs/{file_id}` | 删除文件元数据;存在非终态任务引用时返回 `409 TASK_IN_PROGRESS` | ✓ |

上传限制:≤ 100MB、≤ 1000 页(2026-08-11 决策:与 CF 免费版上传上限对齐,教材扫描件常超 50MB);校验:文件魔数 + 扩展名 + MIME 三重检查,不合规 → `400 PDF_UPLOAD_INVALID`(审核修复,防解析 DoS)。

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

请求体:`{ file_id, chapter_ids[], generation_config }`。样卡不入库、不参与统计(响应返回 SampleCard 轻量组件,见 3.13)。

### 6.4 任务(FR-06/07/12)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/tasks` | 接受样卡 → 创建任务(`PENDING + stage=PLANNING`,创建即返回,幂等保持),规划 worker 异步接管;任务预算超全局硬上限 → `400 VALIDATION_ERROR`(不创建) | ✓ |
| GET | `/v1/tasks/{task_id}` | 长任务轮询:状态、stage(`PLANNING/GENERATING/SCORING`)、已生成数、批次进度、失败码、是否可继续(FR-18) | - |
| POST | `/v1/tasks/{task_id}/resume` | 断点续传,仅处理未完成批次 | ✓ |
| POST | `/v1/tasks/{task_id}/cancel` | 取消任务 | ✓ |

### 6.5 牌组与卡片(FR-03/14)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/decks` | 牌组列表(含进度摘要) | - |
| POST | `/v1/decks` | 新建牌组 `{ name }` | ✓ |
| PATCH | `/v1/decks/{deck_id}` | 牌组改名 `{ name }`;`version` 递增供缓存刷新;返回含真实进度 | ✓ |
| GET | `/v1/decks/{deck_id}` | 详情 + 进度(card_count / due_count / mastered / review_count / mastery_ratio) | - |
| DELETE | `/v1/decks/{deck_id}` | 删除牌组及其卡片、复习状态与统计;重复提交安全返回;存在非终态任务引用时返回 `409 TASK_IN_PROGRESS` | ✓ |

删除后 `tasks.deck_id` 置空,历史任务保留。
列表接口 MVP 暂不分页:`GET /v1/decks` 与 `GET /v1/decks/{deck_id}/cards` 返回全量;单牌组卡片量超 1000 张后再引入分页。
| POST | `/v1/decks/{deck_id}/cards` | 手动新增卡片 `{ front, back }`,分配 position | ✓ |
| POST | `/v1/decks/{deck_id}/cards/import` | 批量导入 `{ cards: [{ front, back }] }`,原子写入,返回逐张结果 | ✓ |
| PATCH | `/v1/cards/{card_id}` | 编辑卡片 `{ front, back }`(2026-08-11 决策:内容覆盖 + **ReviewState 重置为新卡**,与重写同语义;`version` 递增) | ✓ |
| DELETE | `/v1/cards/{card_id}` | 删除单卡(级联清理 review_states / review_events;重复提交安全返回) | ✓ |

导入规则:客户端负责文本解析与预览编辑;服务端仅接收最终确认列表;无法识别的行由客户端在预览阶段拦截(PRD 5.14)。失败整体回滚(`422 IMPORT_PARSE_ERROR`),不产生部分成功;`results` 仅在全成功时返回逐条 `CREATED` 结果,`FAILED` 枚举为预留形态(当前实现不产生)。

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

### 6.9 质量观测(FR-10/11,AC-07;审核修复)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/tasks/{task_id}/batches` | 返回批次列表(Batch),含单卡 Rubric 汇总、质量分布与 Prompt Cache 记录,供测试与联调核验(AC-07) | - |

MVP 无可视化后台;观测数据经此接口 + 卡片详情(Rubric 单卡字段,3.9)核验。

### 6.10 质量聚合观测（O-4，审核设计补全）

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/observability/quality-summary?group_by=model\|pdf\|difficulty&days=30` | 跨任务质量聚合:Rubric 各维平均分、覆盖/重复率均值、任务完成率、成本汇总;按 group_by 分组 | - |

- 隔离口径：按当前 `user_id` 聚合（与业务数据同隔离）；跨用户聚合留给未来运营后台。
- **分组键定义**：`model` = `Batch.model`；`pdf` = 任务所属 `file_id`；`difficulty` = `Batch.generation_unit_id` → 对应单元的 `target_difficulty`（**不能只依赖 Card**，否则生成失败、没有 Card 的 coverage=0 批次丢失；单元缺失/锚定缺失 → `unknown`）。聚合结果按 `rubric_version` 拆子组——查询窗口同时包含多版本时不得无标识混算。
- **评分样本口径**：各评分维度只以对应字段非 NULL 的卡为分母（NULL 不计 0 分、不进分母）；`eligible_card_count` = 经批次归属的卡数，`scored_card_count` = `rubric_total_score` 非 NULL 的卡数，`sampling_rate` = `scored/eligible`（分母 0 时返回 null）；覆盖/重复率均值含 SKIPPED 批次（coverage=0 计入分母）。
- 成本汇总（O-6）：按"价格配置常量"换算 `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens` 为估算金额，hit/miss/output 分开计价；价格常量取 DeepSeek 官方定价、标注生效日期，不固化进 DB。**成本口径：`cost_estimate` 为 generation-stage-only**（Batch token 列为生成阶段兼容投影），不得冒充全链路成本；全链路成本按 `llm_call_attempts` 账本分 stage 汇总，禁止把 Batch 投影再次相加造成双计。
- `/healthz`（存活）、`/readyz`（就绪:DB 连接 + 存储可写，失败 503）、`/metrics`（Prometheus 文本）、`/openapi.json`（接口文档，前端对接在线拉取）为运行观测基础端点，**豁免 Bearer 鉴权**（探针/采集器无账号上下文）；`POST /auth/register`、`POST /auth/login` 同样豁免（6.11）。

### 6.11 账号（FR-19,决策 D-05;V2.2 新增）

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/auth/register` | 创建用户并建立会话;`{ username, password }`;201 返回 AuthSessionResponse(3.15);用户名冲突 → `409 USERNAME_TAKEN` | - |
| POST | `/v1/auth/login` | 校验凭据并建立新会话;200 返回 AuthSessionResponse;失败统一 `401 INVALID_CREDENTIALS` | - |
| POST | `/v1/auth/logout` | 撤销当前会话(仅当前);204 | 幂等键(并发双发单副作用) |
| GET | `/v1/auth/me` | 返回当前用户最小资料 AuthUser(3.14) | - |

规则:register/login 豁免 Bearer 鉴权与 Idempotency-Key;logout/me 需要 Bearer。客户端不得自动重试
register/login(防网络重放静默创建多条会话)。受保护接口 401(`AUTH_REQUIRED` / `AUTH_INVALID`)携带
`WWW-Authenticate: Bearer`(1.4)。logout 的顺序重放(撤销后同键重试)因撤销 token 统一 401 在认证
闸门先行不可达,幂等键保证并发双发单副作用(条件更新天然幂等);login 的非法格式用户名按输入
校验惯例返回 400 VALIDATION_ERROR(非 401),不泄露账号存在性。
认证语义(凭据/会话/限流)见 1.1 / 1.6;数据归属与隔离见 1.1 归属声明与 1.3 幂等约定。

## 7. 错误码表

| 分组 | 错误码 | HTTP | 说明 |
| --- | --- | --- | --- |
| 通用 | `VALIDATION_ERROR` | 400 | 请求结构/字段非法(含 `device_timezone` 非 IANA 时区) |
| | `RATE_LIMITED` | 429 | 限流(1.6),响应携带 `Retry-After` |
| | `IDEMPOTENCY_CONFLICT` | 409 | 幂等键相同但请求体与首次不一致 |
| | `INTERNAL_ERROR` | 500 | 未预期错误(内部细节仅进日志) |
| 账号 | `AUTH_REQUIRED` | 401 | 缺失 Authorization Bearer 凭据;401 带 `WWW-Authenticate: Bearer` |
| | `AUTH_INVALID` | 401 | token 非法/未知/撤销/过期;401 带 `WWW-Authenticate: Bearer` |
| | `INVALID_CREDENTIALS` | 401 | 登录失败(用户名不存在与密码错误统一返回,不暴露账号存在性) |
| | `USERNAME_TAKEN` | 409 | 注册用户名已被占用 |
| PDF | `PDF_UPLOAD_INVALID` | 400 | 非 PDF / 损坏 / 超限(100MB / 1000 页) |
| | `PDF_PARSE_FAILED` | 422 | 文本层解析失败 |
| | `PDF_TOC_MISSING` | 422 | 无可用目录结构(终止流程) |
| | `PDF_NOT_FOUND` | 404 | 不存在或非本用户(统一 404,不暴露存在性) |
| | `CHAPTER_NOT_FOUND` | 404 | 章节不存在或非本文件/本用户(统一 404) |
| API Key | `API_KEY_UNAVAILABLE` | 502 | Key 缺失/解密失败、chat 上游 401/429/5xx 或校验链路(validate_key)上游不可用(含网络);生成链路中 401(Key 错误)不可重试 → 任务 `FAILED`,429/5xx 可重试(账本预算内);生成链路网络/超时与响应解析失败内部记 `GENERATION_FAILED`(重试预算同) |
| | `API_KEY_NOT_SET` | 422 | 样卡 / 任务启动时未保存 Key |
| 任务 | `TASK_NOT_FOUND` | 404 | |
| | `TASK_STATE_CONFLICT` | 409 | 非法状态转移(并发 resume、重复完成) |
| | `TASK_NOT_RESUMABLE` | 409 | 任务不可继续 |
| | `TASK_IN_PROGRESS` | 409 | 删除保护:资源被非终态任务引用(PDF / 牌组) |
| | `GENERATION_FAILED` | 500 | 系统级生成失败(任务 FAILED);批次级失败不产生错误响应 |
| 牌组/卡片 | `DECK_NOT_FOUND` | 404 | 不存在或非本用户(统一 404,不暴露存在性) |
| | `CARD_NOT_FOUND` | 404 | |
| | `GENERATION_ITEM_CONFLICT` | 409 | `generation_item_id` 已对应其他卡 |
| | `IMPORT_PARSE_ERROR` | 422 | 导入内容非法(逐行错误随响应返回;客户端预览阶段已拦截为主) |
| | `REWRITE_SCHEMA_INVALID` | 422 | 单卡重写的新版本未通过 Schema 校验(原卡保留) |
| 复习 | `REVIEW_EVENT_INVALID` | 400 | 评级非法 |
| | `REVIEW_EVENT_CONFLICT` | 409 | 同 `client_event_id` 但 `card_id` / `rating` 与首次不一致 |

注:API Key 校验结果(`INVALID` / `INSUFFICIENT_BALANCE`)经 `200 + ApiKey.status` 返回,不产生错误响应(见 6.2)。跨用户资源访问一律返回 404,不暴露资源存在性(1.1)。
注:生成链路重试分类(适配层 `retryable` 元数据,1.4)——chat 401(Key 错误)非重试(任务 `FAILED`);chat 429/5xx 记 `API_KEY_UNAVAILABLE` 重试,网络/超时与响应解析失败记 `GENERATION_FAILED` 重试;Schema/锚定非法属业务重试,预算同为每操作 2 次重试 = 3 次尝试,由 `llm_call_attempts` 账本计数。

## 8. 运行可观测性（观测范围仅 DeepSeek API）

### 8.1 结构化日志（O-1）

- JSON 单行格式；字段:`timestamp`(ISO 8601 UTC) / `level` / `request_id` / `user_id` / `task_id` / `batch_id` / `error_code` / `message`。
- 级别规范:INFO(请求进出、批处理完成)、WARN(重试、限流触发)、ERROR(异常 + error_code)。
- 贯穿机制:中间件生成 `request_id` 贯穿全链路;后台批处理以 `task_id` + `batch_id` 关联。
- 红线保留:1.5/7.1 的 API Key、完整 PDF 内容、完整 Prompt 不落日志。

### 8.2 健康检查（O-2）

`GET /healthz` 存活探针;`GET /readyz` 就绪探针(DB 连接 + 存储可写,失败 503);`GET /openapi.json` 接口文档;豁免 Bearer 鉴权。

### 8.3 指标（O-3）

`GET /metrics`(Prometheus 文本格式),豁免 Bearer 鉴权,生产子域名默认不暴露:

| 指标 | 类型 | labels |
| --- | --- | --- |
| `generation_tasks_total` | counter | result(COMPLETED/FAILED/CANCELLED) |
| `generation_tasks_duration_seconds` | histogram | - |
| `batch_retry_total` | counter | - |
| `rate_limit_hit_total` | counter | scope(write/ip/auth/api_key/samples/pdf)  # 1.6 维度命名，与实现一致（V2.2：write 维度键为 user；新增 auth 维度） |
| `llm_requests_total` | counter | model(DeepSeek 模型族)/http_status |
| `llm_request_duration_seconds` | histogram | model |
| `llm_tokens_total` | counter | kind(cache_hit/cache_miss/output) |
| `http_requests_total` | counter | method/path/status |
| `http_request_duration_seconds` | histogram | - |

### 8.4 成本观测（O-6）

- 原始 token 数据(`cache_hit_tokens` / `cache_miss_tokens` / `output_tokens`)落 Batch 表,为**生成阶段兼容投影**;全阶段 token 以 `llm_call_attempts` 账本为权威(分 stage 汇总)。
- 估算成本在聚合时按"价格配置常量"换算;常量取 DeepSeek 官方定价、标注生效日期;价格调整只改配置,不动历史数据。
- 出口:8.3 `llm_tokens_total` 与 6.10 聚合接口的成本汇总(`cost_estimate` 标注 generation-stage-only,见 6.10)。
- 事前价格预估(`/tasks/estimate` 与 token 用量估算模型)已删除(2026-08-12 用户拍板),由任务级全局硬上限与 6.10 事后成本观测替代。

### 8.5 评估骨架（O-5）

- Rubric 评分执行者:LLM-as-judge;评分在独立 SCORING 阶段执行(4.1),评分 Prompt 资产入口:
  `agent_evolution/manifest.json` 的 `prompts.scoring`。
- 当前资产登记:Planner Prompt v3 / planner-output Schema v2;Generator Prompt v3 /
  generator-output Schema v2 / 投影后 card Schema v1;Rewrite Prompt v3 / generator-output
  Schema v2 / 投影后 card Schema v1;Scoring Prompt v2 / scoring-output Schema v2 / Rubric v2。
  具体 path 以 manifest 为唯一权威,禁止运行时绕过 manifest 读取相对路径。
- `rubric_version` / `prompt_version` / `schema_version` 按每次调用实际使用的入口记录,不能用
  单个全局 schema_version 混写 card v1 与 generator/planner/scoring output v2。
- **调用账本(`llm_call_attempts`)逐调用记录**实际使用的 `prompt_name/prompt_version`、
  `schema_name/schema_version`、`rubric_version`(不适用列 NULL)以及 usage/状态/错误码;
  账本是重试预算、调用上限与全阶段 token 的权威(4.2/6.10/8.4)。
- 评分请求记录:prompt 版本 + 输入摘要 + 输出分;不落完整 prompt。

## 9. 与 PRD 的对照

| 契约章节 | PRD 章节 | 状态 |
| --- | --- | --- |
| 1.1 数据主体 | V2.2 FR-19 / AC-12、D-05 / D-06 | 一致(V2.2 账号会话取代 D-02) |
| 1.5 API Key | 5.17 / 7.1 / AC-11、D-03 | 一致(PRD 已同步修订) |
| 3.9 判断题结构 | 5.8 / D-01 | 一致 |
| 5 复习排程 | 5.15 / 6.6 / AC-10 | 一致(PRD 已同步修订为 FSRS) |
| 6.7 单卡重写 | 5.13 / AC-06、D-04 | 一致 |
| 5.3 掌握判定 | 5.15 牌组进度 / 5.16 / 6.5 | 一致(PRD 已同步修订) |
| 1.6 限流 / 1.7 传输与保留 | 7.1 数据安全 / 5.1 | 新增(审核修复) |
| 6.1 章节修改与上传限制 | 4.1 第 3 步 / FR-02 / AC-02 | 新增(审核修复) |
| 6.9 质量观测 | FR-10 / FR-11 / AC-07 | 新增(审核修复) |
| 3.9 Rubric 与关联字段 | 5.9 / 5.6 / 6.3 / AC-07 | 修复(审核) |
| 8 运行可观测性 / 6.10 聚合观测 | PRD 8 核心指标 / FR-10 / FR-11 | 新增(设计规格 6422765) |
| 3.5/3.6 生成单元与锚定 / 3.7 批=单元 / 4.1 任务状态机 / 6.10 分组键 | 5.4.1 / 5.6 / 5.7 | 一致(LLM 链路升级工作包契约同步) |
| 3.14/3.15 账号与会话 / 6.11 账号接口 / 1.6 限流 / 7 账号错误码 | V2.2 FR-19 / AC-12、D-05 | 新增(账号登录工作包契约同步) |
