# 结构契约 v2.5

前后端接口合同。需求依据:[PRD v2.5](../PRD/V2.5/prd_v2_5.md)(增量继承 [v2.4](../PRD/V2.4/prd_v2_4.md)、[v2.3](../PRD/V2.3/prd_v2_3.md)、[v2.2](../PRD/V2.2/prd_v2_2.md)、[v2.1](../PRD/V2.1/prd_v2_1.md));机器可读接口定义:[openapi.yaml](openapi.yaml);持久化映射:[database-design.md](database-design.md);V2.5 目标设计见 [v2.5-target-architecture.md](v2.5-target-architecture.md)。本文件自 V2.5 起为 V2.5 实现事实(V2.5 增量替换语义见 PRD v2.5 §0.3)。

**字段权威声明**:本章第 3 节资源模型是字段定义的唯一来源;`openapi.yaml` schema 与 `database-design.md` 表结构均从本章派生。

## 1. 总则

### 1.1 数据主体与鉴权(决策 D-05)

- 用户经邮箱+密码**注册或登录**获得 opaque Bearer session token(注册另附 username 展示名);受保护请求携带 `Authorization: Bearer <token>`(FR-19)。
- 注册/登录接口(6.11)无鉴权;探针与匿名系统端点(8.2/8.3)豁免 Bearer;其余业务接口全部需要 Bearer。
- 所有资源按 `user_id` 隔离;服务端校验资源归属,禁止仅凭资源 ID 访问他人数据;跨用户访问统一 404,不暴露存在性。
- 缺失/非法/撤销/过期 token → `401 AUTH_REQUIRED` / `AUTH_INVALID`,一律携带 `WWW-Authenticate: Bearer` 响应头。
- **凭据规则**:登录凭据为邮箱+密码——邮箱 3~254 位(含 `@`、无空白)、服务端统一转小写、全库唯一;username 为展示名 1~24 位、中文/字母/数字/`._-`、可重名、不强制小写;密码 8~128 字符、不截断、不做 normalization;密码 Argon2id(≥ memory_cost=19456 KiB / time_cost=2 / parallelism=1);登录失败统一 `401 INVALID_CREDENTIALS`(邮箱不存在时做固定 dummy 校验),邮箱冲突 `409 EMAIL_TAKEN`。
- **会话规则**:256-bit 随机 opaque token,数据库只存 SHA-256 摘要;默认 30 天有效期,V2.4 起活跃滑动续期——每次有效请求后剩余不足 29 天即续至 now+30 天(按天节流),连续 30 天不活跃过期;logout 只撤销当前会话;同一用户允许多会话。
- **风险声明**:session token 等同于密码,泄漏后在被撤销或过期前可被冒用;缓解:注册/登录按 IP 与邮箱限流(1.6)、token 只存摘要、敏感信息不落日志(1.5/8.1)。
- **归属声明**:所有资源隐含归属当前登录 `user_id`,持久化列为 `user_id`;无 `user_id` 列的表(chapters、knowledge_points 等)经外键关联路径归属校验。V2.3 起设备架构已彻底清除——`devices` 表、`device_id` 列与设备域数据物理删除,owner 恒为 `user_id`(V2.1 历史:曾以 `device_id` 为隔离键;V2.2 曾按决策 D-06 保留旧数据,已撤销)。

### 1.2 时间与时区

- 服务端时间为**权威时钟**:到期判断(`due`)与统计分桶一律使用服务端时间。
- 所有时间字段为 ISO 8601 UTC(RFC 3339),例:`2026-08-10T09:00:00Z`。
- **学习时区(V2.5)**:账号级 `UserPreferences.learning_timezone`(IANA 名称,如 `Asia/Shanghai`)为"今天"、自然周与连续学习天数的唯一权威,所有设备使用同一值;设备时区变化需用户确认更新(3.15)。周起始日为**周一**。客户端不再随请求上报 `timezone`/`weekly_goal`。
- 复习事件的 `device_timezone` 降级为**可空审计字段**(3.11),不参与权威统计分桶;排程计算继续使用 UTC。

### 1.3 幂等约定

- 所有写操作(创建、追加、删除、任务启动、放弃、重试、评级、撤销、重写预览创建/应用、偏好与设置更新、章节确认)必须携带请求头 `Idempotency-Key`(客户端生成 UUID v4)。
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
    "localization_key": "error.deck_not_found",
    "actions": ["VIEW_TASKS"],
    "details": {"task_ids": []}
  }
}
```

- 错误码为稳定字符串,客户端按 `localization_key` 映射文案;不随消息文本变化。
- 受保护接口的 401(`AUTH_REQUIRED` / `AUTH_INVALID`)携带 `WWW-Authenticate: Bearer` 响应头(1.1);登录失败的 `INVALID_CREDENTIALS` 不要求该头。
- `message` 仅面向用户展示,不得包含堆栈、内部路径、SQL 或他人数据;内部细节进服务端日志(以 `request_id` 关联)。
- `actions` 与 `details` 为可选字段;资源删除冲突时服务端返回可执行动作(`ABANDON_AND_RETRY`、
  `WAIT_FOR_TERMINAL`、`VIEW_TASKS`)及非敏感任务 ID,客户端据此引导用户,不得把预检当作资源锁。
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
| 登录(邮箱分桶) | 按规范化邮箱(默认阈值运维可调) | `POST /auth/login`(防单账号分布式猜测) |
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
| `project_id` | uuid | ✗ | V2.5 归属项目(以 `learning_projects.file_id` 唯一外键为持久化权威,PDF 表不重复存 project_id) |
| `created_at` | datetime | ✓ | |

规则:目录解析失败 → `FAILED` + `error_code`,前端终止流程,不提供 AI 猜测兜底(PRD 5.2)。
删除规则(V2.5):PDF 随学习项目生命周期管理——仅 `PARSE_FAILED` 项目可 `replace-pdf` 原子替换;项目删除时按用户选择随项目删除(3.16/6.2)。旧 `/pdfs` 兼容路径保留过渡期,委托项目接口同一业务语义(6.1 注)。

### 3.3 Chapter

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `chapter_id` | uuid | ✓ | |
| `name` | string | ✓ | 可修改 |
| `start_page` | int | ✓ | 可修改 |
| `end_page` | int | ✓ | 可修改 |

### 3.4 GenerationTask(制卡任务,V2.5 重写)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `task_id` | uuid | ✓ | |
| `project_id` | uuid | 新任务必填 | 归属学习项目;迁移前已失去 PDF 的终态历史任务可为 null(只读历史,不可重试) |
| `file_id` | uuid | 新任务必填 | 项目当前 PDF;历史终态任务可为 null |
| `deck_id` | uuid | 新任务必填 | 目标牌组,必须属于同一项目;删除牌组后置 `null`(任务保留) |
| `retry_of_task_id` | uuid | ✗ | 失败重试关联:只指向同用户失败任务 |
| `status` | enum | ✓ | 七态,见 4.1 |
| `internal_stage` | enum | ✗ | `PLANNING` / `GENERATING` / `SCORING` / `PUBLISHING`,仅运行期内部观测,不直接作为用户状态 |
| `selected_chapters` | Chapter[] | ✓ | 开始正式生成前冻结快照(快照冻结语义见 4.1) |
| `generation_config` | GenerationConfig | ✓ | 任务独立配置,见 3.5 |
| `sample_cards` | SampleCard[] | ✗ | 持久化 1~3 张样卡;配置变化时清空(见 4.1) |
| `sample_config_hash` | string | ✗ | 样卡对应的配置指纹,防止确认过期样卡 |
| `sample_confirmed_at` | datetime | ✗ | 样卡确认时间 |
| `generated_card_count` | int | ✓ | 只统计已发布卡;失败任务为 0 |
| `error_code` | string | ✗ | 用户安全失败码 |
| `failure_stage` | enum | ✗ | `PLANNING` / `GENERATING` / `SCORING` / `PUBLISHING` |
| `created_at` / `started_at` / `ended_at` | datetime | 按需 | |
| `updated_at` | datetime | ✓ | 长任务轮询刷新区分 |

删除规则(V2.5,PRD 7.3):删除任务默认只删历史——已发布卡的 `source_task_id` 置空;用户选择连同结果删除时才删除该任务的已发布卡及复习数据。失败任务遗留的 `STAGED` 卡在删除任务时级联清理,绝不能转为无来源可见卡。

### 3.5 GenerationConfig(V2.5 重写)

```json
{
  "coverage_mode": "BALANCED",
  "difficulty_ratio": { "basic": 40, "understanding": 40, "deep_question": 20 },
  "custom_requirements": ""
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `coverage_mode` | enum | ✓ | `COMPACT`(精简) / `BALANCED`(均衡) / `EXTENSIVE`(充分覆盖);表达知识覆盖深度,不显示、不承诺卡片数量;隐藏安全硬上限仍由代码控制(PRD 5.4.1) |
| `difficulty_ratio` | object | ✓ | `basic / understanding / deep_question` 为 0~100 的 10% 整数档,合计 100,允许任一档为 0;比例为 0 的难度不生成单元和样卡 |
| `custom_requirements` | string | ✗ | 仅当前任务生效 |

难度枚举:`BASIC`(基础记忆) / `UNDERSTANDING`(理解分析) / `DEEP_QUESTION`(开放深问,原 APPLICATION 改名,PRD 5.6~5.7)。`DEEP_QUESTION` 只允许 `QUESTION` 卡型,背面为参考思路,不提供唯一标准答案;判断题只属于前两档(组合规则见 3.6)。
规则(V2.5):覆盖深度与整数比例默认值按账号跨设备保存于 `UserPreferences`(3.15),创建任务时服务端落默认值快照;配置变更会失效既有样卡(见 4.1)。

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
| `target_difficulty` | enum | ✓ | `BASIC` / `UNDERSTANDING` / `DEEP_QUESTION`(规划锚定,旧数据为 null;历史 `APPLICATION` 经迁移映射为 `DEEP_QUESTION`) |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE`(规划锚定,旧数据为 null) |
| `priority` | int | ✓ | 全局顺序(服务端按章序、组序、组内数组顺序合并分配,Planner 不输出数值) |
| `status` | enum | ✓ | `PENDING` / `PROCESSED` / `SKIPPED` |

**双维锚定**:卡型与目标难度是两个独立维度,规划时同时锚定、生成时遵循,互不派生。Generator 输出卡型不符合锚定 → 代码校验拒绝(进入该单元重试预算);`Card.target_difficulty` 由服务端写规划锚定值(生成时落库,评分时不再补写),不要求模型回传;内容是否达到目标难度由 Rubric 观测。

**组合规则**(难度 × 卡型,规划锚定的形态约束):

| 难度 | 卡型 | 形态 | 约束 |
| --- | --- | --- | --- |
| BASIC | QUESTION(默认) / TRUE_FALSE | 原子事实/定义直问或二值判断 | 判断题表述无歧义 |
| UNDERSTANDING | QUESTION / TRUE_FALSE | 对比/推理/因果 | 同上 |
| DEEP_QUESTION | QUESTION(唯一) | 开放深问(普通问答卡,背面为参考思路) | 不提供唯一标准答案;不要求聚合多个原子知识点 |

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

### 3.8 Deck(V2.5 增量)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `deck_id` | uuid | ✓ | |
| `name` | string | ✓ | |
| `source` | enum | ✓ | `MANUAL` / `IMPORTED` / `GENERATED`(牌组本身的来源;`GENERATED` 为 PDF 制卡新建的归属牌组) |
| `project_id` | uuid | ✗ | V2.5 归属学习项目;`null` 表示手动/独立牌组 |
| `card_count` | int | ✓ | 派生进度(接口计算,只含可见卡,可见谓词见 3.9) |
| `due_count` | int | ✓ | 派生:`due <= now` 的可见卡数 |
| `mastered_card_count` | int | ✓ | 派生:掌握判定见 5.3 |
| `review_count` | int | ✓ | 派生:累计复习事件数 |
| `mastery_ratio` | float | ✓ | 派生:`mastered_card_count / card_count`,为 0 时返回 0 |
| `created_at` / `updated_at` / `version` | - | ✓ | `version` 供客户端刷新缓存 |

### 3.9 Card(V2.5 增量)

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
| `source_task_id` | uuid | ✗ | V2.5 生成来源任务;删历史保留卡时置空 |
| `chapter_id` | uuid | ✗ | V2.5 源章节;null 在项目内显示"未归属章节",不另建伪章节行 |
| `publication_state` | enum | ✓ | V2.5 `STAGED` / `PUBLISHED`;历史卡均迁为 PUBLISHED |
| `delete_batch_id` | uuid | ✗ | V2.5 非空表示 10 秒待删除批次 |
| `pending_delete_at` / `undo_until` | datetime | ✗ | V2.5 服务端计时 |
| `target_difficulty` | enum | ✗ | `BASIC` / `UNDERSTANDING` / `DEEP_QUESTION`;仅 `GENERATED` 卡;生成入库时由服务端写入(评分时不再补写;3.6 双维锚定);手动/导入为 null |
| `knowledge_point_ids` | uuid[] | ✗ | 仅 `GENERATED` 卡;关联生成单元(兼容列;本工作包起生成路径不再写多知识点聚合) |
| `evidence_score` / `correctness_score` / `difficulty_score` / `learning_value_score` | int | ✗ | Rubric 各维度 0~3,仅 `GENERATED` 卡(PRD 5.9) |
| `rubric_total_score` | int | ✗ | Rubric 总分 0~12,仅 `GENERATED` 卡 |
| `version` | string | ✓ | 变更版本,客户端缓存刷新用(重写时递增,决策 C-05) |
| `created_at` / `updated_at` | datetime | ✓ | |

决策 D-01 说明:判断题保留结构化字段(`statement` / `answer_boolean` / `explanation`),同时后端填充 `front` / `back` 文本供前端通用渲染;判断题专用渲染后续版本补充。手动 / 导入卡 `card_type = QUESTION`,仅用 `front` / `back`。

**统一可见谓词(V2.5)**:卡片可见条件恒为 `publication_state = PUBLISHED AND delete_batch_id IS NULL`。所有列表、到期队列、今日计划、统计和进度聚合必须复用同一查询谓词,禁止各模块自行漏写过滤条件;`STAGED` 卡在任务整体成功前对任何用户侧查询不可见(3.4/4.1)。

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
| `device_timezone` | string | ✗ | V2.5 降级为可空审计字段,不参与权威统计(1.2) |
| `created_at` | datetime | ✓ | |

不可变记录;离线补传或重试(同一 `client_event_id`)不重复计数。

### 3.12 StatsDashboard(数据看板,V2.5 重写)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `period` | object | ✓ | `{ "start": datetime, "end": datetime, "week_ordinal": int }` 统计周期 |
| `timezone` | string | ✓ | 实际分桶时区 = 账号学习时区(1.2) |
| `weekly_activity` | int[7] | ✓ | 周一~周日每日复习事件数(按学习时区分桶) |
| `weekly_total` | int | ✓ | 本周总复习事件数 |
| `weekly_completed_count` | int | ✓ | V2.5 本周不同 `(账号学习日期, card_id)` 数 |
| `week_change_rate` | float \| null | ✓ | `(本周-上周)/上周`;上周为 0 时 null(客户端显示"暂无对比") |
| `weekly_goal` | int | ✓ | V2.5 服务端派生:`daily_learning_goal × 7`(3.15);不再接受客户端参数 |
| `weekly_goal_progress` | float | ✓ | `min(weekly_completed_count / weekly_goal, 1)` |
| `updated_at` | datetime | ✓ | 聚合快照生成时间(PRD 6.6:客户端据此判断缓存是否过期) |
| `recall_accuracy` | float \| null | ✓ | 周期内 GOOD 事件 / 全部事件 |
| `first_answer_accuracy` | float \| null | ✓ | 首次评级为 GOOD 的卡数 / 首次复习卡数 |
| `retention_rate` | float \| null | ✓ | 非首次事件中 GOOD 数 / 非首次事件数 |
| `streak_days` | int | ✓ | 截至账号学习时区当天连续有复习事件的自然日数(按学习时区分桶) |
| `mastered_card_count` | int | ✓ | 掌握卡片数(见 5.3) |
| `has_data` | bool | ✓ | false 时客户端展示空态,不得用固定示例值 |

**分母为 0 的比率一律返回 `null`**,不得以 0% 冒充;无历史数据时不得伪造固定日期数组或伪 0%。时区改变后用 UTC `reviewed_at` 重新分桶,不改写事件。

### 3.13 SampleCard(V2.5 增量)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `card_id` | uuid | ✓ | 样卡预览标识 |
| `front` / `back` | string | ✓ | 通用渲染字段(所有卡片) |
| `code` | string | ✗ | 卡片编号 |
| `card_type` | enum | ✓ | `QUESTION` / `TRUE_FALSE` |
| `question` / `answer` | string | ✗ | 仅 `QUESTION` 卡 |
| `statement` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `answer_boolean` | bool | ✗ | 仅 `TRUE_FALSE` 卡 |
| `explanation` | string | ✗ | 仅 `TRUE_FALSE` 卡 |
| `target_difficulty` | enum | ✗ | `BASIC` / `UNDERSTANDING` / `DEEP_QUESTION` |

与 Card 的差异：删去落库/归属/版本语义字段（deck_id、position、source、
generation_item_id、knowledge_point_ids、Rubric 四维与总分、version、created_at、
updated_at）——样卡仅承载前端预览所需结构。

V2.5 规则:样卡**持久化**于任务(3.4),只为比例大于 0 的难度各生成 1 张,共 1~3 张,
不固定卡型数量(PRD 5.5);样卡不入库为 Card、不参与统计与 Rubric;配置变化时服务端清空
既有样卡并要求重新生成(4.1)。

### 3.14 AuthUser(账号,V2.2 新增;V2.5 增量)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user_id` | uuid | ✓ | 数据主体标识;全部业务资源的归属键(1.1) |
| `username` | string | ✓ | 1~24 位展示名,中文/字母/数字/._-,可重名 |
| `email` | string | ✓ | V2.5 只读返回规范化后的当前登录邮箱;不可 PATCH |
| `avatar_key` | enum | ✓ | V2.5 `mood_01`~`mood_12`,只接受内置预设;默认 `mood_01` |
| `created_at` | datetime | ✓ | |

规则:不返回密码/hash;username 为展示名,可重名、不参与登录;登录键为 email(唯一性以转小写后的规范化值为准,1.1)。V2.5 `PATCH /auth/me` 仅接受 `{ username?, avatar_key? }`,至少一个字段。

### 3.15 UserPreferences(账号偏好,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `default_coverage_mode` | enum | ✓ | `COMPACT` / `BALANCED` / `EXTENSIVE`,默认 `BALANCED` |
| `default_difficulty_ratio` | object | ✓ | `basic/understanding/deep_question` 为 0~100 的 10% 整数档,合计 100,允许任一档为 0;默认 `40/40/20` |
| `daily_learning_goal` | int | ✓ | 10~200,10 的倍数,默认 50 |
| `learning_timezone` | string | ✓ | 有效 IANA 时区,账号级权威(1.2) |
| `current_project_id` | uuid \| null | ✓ | 当前学习项目;项目删除时服务端置空 |
| `updated_at` | datetime | ✓ | 最后成功保存时间 |

规则:比例、目标、IANA 时区服务端校验(`INVALID_PREFERENCES` / `INVALID_LEARNING_TIMEZONE`);偏好跨设备同步;主题仍为客户端本机偏好,不进入此资源。部分更新 last-success-wins。

### 3.16 LearningProject(学习项目,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | uuid | ✓ | 用户内资源标识 |
| `name` | string | ✓ | 去首尾空白后 1~60 字符,可重名;默认取上传文件名去扩展名 |
| `file` | PdfFile | ✓ | 当前 PDF;列表响应可只返回摘要 |
| `status` | enum | ✓ | `PARSING` / `PARSE_FAILED` / `AWAITING_CHAPTER_CONFIRMATION` / `READY` |
| `chapter_count` | int | ✓ | 派生 |
| `deck_count` | int | ✓ | 派生 |
| `task_count` | int | ✓ | 派生 |
| `tasks` | Task[] | ✗ | 仅项目详情可选返回;列表可省略,用于恢复草稿/样卡/生成中任务 |
| `created_at` / `updated_at` / `version` | - | ✓ | 缓存刷新与并发检查 |

规则:一个项目恰好对应一份当前 PDF;解析失败时允许 `replace-pdf` 原子替换该 PDF。`status` 由 PDF 状态与 `chapters_confirmed_at` 确定,不建立可漂移的第二套状态列。删除保护见 6.2;项目删除时 `user_preferences.current_project_id` 置空。`tasks` 仅为详情的服务端任务快照,权威状态仍来自任务表/任务列表。

### 3.21 DeletionPreflight(删除预检,V2.5 工程化增量)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `resource_type` | enum | ✓ | `PROJECT` / `DECK` |
| `resource_id` | uuid | ✓ | 待删除资源 |
| `can_delete` | bool | ✓ | 当前快照下是否无阻塞 |
| `blockers` | DeletionTaskBlocker[] | ✓ | 活跃任务摘要与允许动作 |
| `abandonable_task_ids` | uuid[] | ✓ | `DRAFT`/样卡阶段可在删除事务内放弃的任务 |
| `has_uncancellable_tasks` | bool | ✓ | 是否存在正式 `GENERATING` 任务 |
| `actions` | string[] | ✓ | `ABANDON_AND_RETRY` / `WAIT_FOR_TERMINAL` / `VIEW_TASKS` |
| `impact` | object | ✓ | 资源数量与当前项目状态;仅展示用途,不作为删除授权 |

预检是只读建议;实际删除必须在同一写事务内重新检查并用 CAS 放弃可放弃任务。正式生成中不可强制终止。

### 3.17 ProjectStudySettings(项目学习设置,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `selected_new_card_chapter_ids` | uuid[] | ✓ | 只限制新卡;空数组表示暂无新卡范围 |
| `include_unassigned` | bool | ✓ | 是否包含 `chapter_id = null` 的新卡 |
| `updated_at` | datetime | ✓ | |

规则:一项目一行;到期卡覆盖当前项目全部已学习卡,不受本章范围限制。

### 3.18 CardDeletionBatch(卡片删除批次,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `delete_batch_id` | uuid | ✓ | 用户内标识 |
| `card_ids` | uuid[] | ✓ | 服务端返回;数据库以 Cards 外键关系为权威 |
| `undo_until` | datetime | ✓ | 服务端接受最后一次追加后 10 秒 |
| `status` | enum | ✓ | `PENDING` / `UNDONE` / `FINALIZED` |
| `created_at` / `updated_at` | datetime | ✓ | |

规则:向仍为 `PENDING` 的批追加卡时,服务端原子更新整批 `undo_until = now + 10s`;撤销在同一事务清空所有卡片删除标记并置 `UNDONE`;过期批由后台清理器或任意相关读取前的惰性清理最终硬删除,两种路径必须幂等;V2.5 无回收站。

### 3.19 CardRewritePreview(单卡 AI 重写预览,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `rewrite_id` / `card_id` | uuid | ✓ | |
| `base_card_version` | string | ✓ | 应用时乐观并发校验 |
| `front` / `back` / `card_type` / `target_difficulty` | - | ✓ | 预览内容 |
| `custom_requirements` | string \| null | ✓ | 不保存完整 Prompt |
| `status` | enum | ✓ | `PENDING` / `APPLIED` / `CANCELLED` / `EXPIRED` |
| `expires_at` | datetime | ✓ | 24 小时;实现常量统一 |

规则:只允许来源项目、PDF、章节和来源页仍存在的 `GENERATED` 卡创建预览;应用时同事务更新卡片、递增版本并重置 ReviewState;版本已变化返回 `409 CARD_VERSION_CONFLICT`,原卡不变;取消幂等。

### 3.20 TodayStudyPlan(今日学习计划,V2.5 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `timezone` | string | ✓ | 账号学习时区 |
| `study_date` | string | ✓ | 账号学习时区下的学习日期 |
| `current_project` | LearningProject \| null | ✓ | 无当前项目时返回 null(空态) |
| `daily_goal` | int | ✓ | 服务端偏好 |
| `today_completed_count` | int | ✓ | 今日去重完成数(同一 `(账号学习日期, card_id)` 只计一次) |
| `due_count` | int | ✓ | 到期总数 |
| `main_plan_remaining` | int | ✓ | 主计划剩余数 |
| `backlog_count` | int | ✓ | 积压数 |
| `cards` | TodayPlanCard[] | ✓ | 有序卡片列表 |

主计划(4.5):当前项目全部已学习且到期的可见卡 → 按 `遗忘风险 DESC → 逾期时长 DESC → card_id` 稳定排序取到每日目标 → 仍有余额时从项目学习范围中的 `NEW` 卡按章节顺序、position、card_id 补足。遗忘风险由统一 FSRS 适配器按服务端 `now` 计算;无法计算时风险置 0,再按逾期与稳定键排序。

### 3.15 AuthSessionResponse(会话,V2.2 新增)

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `user` | AuthUser | ✓ | 最小用户资料 |
| `access_token` | string | ✓ | 256-bit 随机 opaque token;仅本响应与客户端安全存储出现,服务端只存 SHA-256 摘要 |
| `token_type` | string | ✓ | 恒为 `Bearer` |
| `expires_at` | datetime | ✓ | 会话绝对过期时间(默认签发 + 30 天);V2.4 起活跃滑动续期(每次有效请求后剩余不足 29 天则续至 +30 天,按天节流) |

规则:register(201)与 login(200)共用本形状;logout 撤销当前会话返回 204;`GET /auth/me` 只返回 `user`。
客户端不得自动重试 register/login(防网络重放静默创建多条会话,FR-19)。

## 4. 状态机

### 4.1 GenerationTask(V2.5 七态)

```text
POST /projects/{project_id}/tasks → DRAFT(自动保存,创建即返回,幂等保持)
DRAFT ──请求样卡──→ SAMPLE_GENERATING ──成功──→ AWAITING_SAMPLE_CONFIRMATION
DRAFT | SAMPLE_GENERATING | AWAITING_SAMPLE_CONFIRMATION ──配置变更──→ DRAFT(样卡清空、hash 置空)
DRAFT | SAMPLE_GENERATING | AWAITING_SAMPLE_CONFIRMATION ──abandon──→ ABANDONED
AWAITING_SAMPLE_CONFIRMATION ──start(校验 sample_config_hash)──→ GENERATING
GENERATING ──整批成功发布──→ COMPLETED(generated_card_count=最终发布数)
GENERATING ──任一失败──→ FAILED(零部分可见)
FAILED ──retry──→ 新任务 DRAFT(retry_of_task_id 指向原任务;正式生成失败可沿用已确认样卡)
```

- **DRAFT 创建与自动保存**:`POST /tasks` 校验章节/牌组同项目归属后写入 DRAFT(章节快照、目标牌组、配置),页面切换、App 退出或换设备后读取服务端最新状态继续,无需重新上传 PDF。配置仅在 `DRAFT` / `AWAITING_SAMPLE_CONFIRMATION` 可改,修改后清空 `sample_cards`、`sample_config_hash`、`sample_confirmed_at`(样卡失效)。
- **样卡生成**:`POST /tasks/{task_id}/samples` 持久化生成 1~3 张样卡(只为比例>0 的难度各 1 张),写入 `sample_config_hash`(配置指纹);幂等键防重复触发。比例全 0 为非法配置(`INVALID_PREFERENCES` 语义,创建/修改时即拒绝)。
- **start 校验**:`POST /tasks/{task_id}/start` 校验当前配置 hash 与 `sample_config_hash` 一致(不一致 → `409 SAMPLE_STALE`)且样卡存在,置 `sample_confirmed_at` 并进入 `GENERATING`。
- **内部阶段**:`internal_stage` 依次 `PLANNING → GENERATING → SCORING → PUBLISHING`,仅为运行期观测,不直接作为用户状态。规划阶段语义沿用:worker CAS 抢占,同一短事务内按快照 `chapter_id` 重读章节最新 `name/start_page/end_page` 覆盖并冻结为规划快照;所选章节已删除或已不属于该 PDF → 任务 `FAILED`(`failure_stage=PLANNING`,内部原因区分 `CHAPTER_SNAPSHOT_STALE`)。
- **STAGED 隔离与整批发布**:正式生成写入的卡均为 `STAGED`(可见谓词 3.9 排除);发布在同一短事务内校验至少一张合法卡 → 全部置 `PUBLISHED` → 任务 `COMPLETED` + `generated_card_count`;任何阶段失败 → 任务 `FAILED`,`STAGED` 卡继续隔离,用户侧零部分可见。0 张有效卡整体失败(`TASK_ZERO_CARDS`,V25-D-23)。
- **失败重试**:`POST /tasks/{task_id}/retry` 只允许失败任务,创建关联新任务(复制已确认配置;正式生成失败可沿用已确认样卡),原失败任务保留并显示关联(PRD 5.13)。
- **用户侧无暂停/取消**:`PAUSED`/`resume`/`cancel` 用户 API 全部删除;执行器内部恢复经同一状态的租约/心跳重新抢占,不暴露用户状态。历史 `PAUSED` 任务迁为 `FAILED` 并写 `LEGACY_PAUSED_TASK`(5.2)。
- **abandon**:`POST /tasks/{task_id}/abandon` 只允许 `DRAFT / SAMPLE_GENERATING / AWAITING_SAMPLE_CONFIRMATION`(正式生成前),进入 `ABANDONED` 终态;`SAMPLE_GENERATING` 时后台请求完成后样卡写入无害。
- **删除保护**:活跃任务(`DRAFT / SAMPLE_GENERATING / AWAITING_SAMPLE_CONFIRMATION / GENERATING`)不允许删除其项目、引用章节或目标牌组;正式生成前可先 abandon,解析或正式生成中需等待终态(`PROJECT_HAS_ACTIVE_TASK` / `TASK_STATE_CONFLICT`)。
- `FAILED`:系统级不可恢复错误(如 API Key 失效、上游持续不可用)或 0 张有效卡;`error_code` 用户安全。批次级失败(Schema 重试达上限)不置 `FAILED` —— 该批 `SKIPPED`,任务继续处理其余批次(见 4.2)。
- 前端页面状态映射:`GENERATING` = 生成中,`COMPLETED` = 完成,`FAILED` = 失败可重试,`ABANDONED` = 已放弃。

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

### 6.1 账号与偏好(V2.5 目标 4.1)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/auth/me` | 返回含 email、avatar_key 的 AuthUser | - |
| PATCH | `/v1/auth/me` | `{ username?, avatar_key? }`,至少一个字段 | ✓ |
| GET | `/v1/preferences` | 返回 UserPreferences(3.15) | - |
| PATCH | `/v1/preferences` | 部分更新;比例、目标、IANA 时区服务端校验 | ✓ |

规则:email 只读不可 PATCH;`avatar_key` 只接受 `mood_01`~`mood_12`;API-key 字段不进入 profile/preferences 载荷。注册/登录/logout 继续沿用 6.12。

### 6.2 学习项目与章节(V2.5 目标 4.2)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/projects` | multipart PDF + 可选 name;上传成功即建立项目(PDF 异步解析) | ✓ |
| GET | `/v1/projects` | 当前用户项目列表,支持真实空态 | - |
| GET | `/v1/projects/{project_id}` | 项目详情(含 file/chapters 摘要与派生计数) | - |
| GET | `/v1/projects/{project_id}/deletion-preflight?retain_decks=true\|false` | 删除预检:影响范围、阻塞任务与可执行动作 | - |
| PATCH | `/v1/projects/{project_id}` | 重命名(1~60 字符,去首尾空白) | ✓ |
| DELETE | `/v1/projects/{project_id}?retain_decks=true\|false&abandon_pre_generation_tasks=true\|false` | 可原子放弃正式生成前任务;正式生成中仍保护 | ✓ |
| POST | `/v1/projects/{project_id}/replace-pdf` | 仅解析失败项目可替换并重新解析(原子替换 PDF) | ✓ |
| PATCH | `/v1/projects/{project_id}/chapters/{chapter_id}` | 修改章节名称 / 起始页 / 结束页 | ✓ |
| DELETE | `/v1/projects/{project_id}/chapters/{chapter_id}?delete_cards=false` | 活跃任务保护;保留卡时 `chapter_id` 置空 | ✓ |
| POST | `/v1/projects/{project_id}/confirm-chapters` | 确认目录,使项目进入 READY | ✓ |
| GET/PATCH | `/v1/projects/{project_id}/study-settings` | 新卡章节范围与未归属分组(3.17) | PATCH ✓ |

上传限制继续适用:≤ 100MB、≤ 1000 页;文件魔数 + 扩展名 + MIME 三重检查,不合规 → `400 PDF_UPLOAD_INVALID`。
兼容路径:旧 `/v1/pdfs*` 在过渡期保留,内部**委托**项目接口同一业务语义,不得创建第二套项目/任务状态。

### 6.3 API Key(FR-17,继承)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| PUT | `/v1/api-key` | 验证并保存,返回 ApiKey(仅状态与脱敏标识) | ✓ |
| GET | `/v1/api-key/status` | 查询状态;未保存时返回 `200` + `status=UNKNOWN`、`masked_key=""` | - |

校验结果一律经 `200 + ApiKey.status` 返回(`INVALID` / `INSUFFICIENT_BALANCE` 属正常业务结果,不产生 422 错误响应);`INVALID` 校验结果**不覆盖**已存在的有效 Key(防冒用者替换他人有效 Key),`AVAILABLE`(更换 Key)才覆盖。

### 6.4 制卡任务(V2.5 目标 4.3)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/projects/{project_id}/tasks` | 建立 `DRAFT`,保存章节、目标牌组和配置(自动保存语义) | ✓ |
| GET | `/v1/tasks?project_id=&status=` | 学习页任务区与历史列表 | - |
| GET | `/v1/tasks/{task_id}` | 任务详情(七态、internal_stage、样卡、失败码) | - |
| PATCH | `/v1/tasks/{task_id}` | 仅 `DRAFT`/`AWAITING_SAMPLE_CONFIRMATION` 可改配置,修改后样卡失效 | ✓ |
| POST | `/v1/tasks/{task_id}/samples` | 持久化生成 1~3 张样卡(比例>0 的难度各 1 张);幂等键防重复触发 | ✓ |
| POST | `/v1/tasks/{task_id}/start` | 校验 `sample_config_hash` 后进入 `GENERATING` | ✓ |
| POST | `/v1/tasks/{task_id}/abandon` | 只允许正式生成前状态,进入 `ABANDONED` | ✓ |
| POST | `/v1/tasks/{task_id}/retry` | 失败任务创建关联新任务(可沿用已确认样卡) | ✓ |
| DELETE | `/v1/tasks/{task_id}?delete_generated_cards=false` | 终态任务;按参数保留或删除已发布卡 | ✓ |

删除用户侧 `/resume`、`/cancel`、暂停状态与按钮;执行器内部恢复经租约/心跳,不暴露 `PAUSED`。

### 6.5 牌组、卡片与撤销(V2.5 目标 4.4)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/decks` | 牌组列表(含进度摘要);支持 `project_id` 归属过滤 | - |
| POST | `/v1/decks` | 新建牌组 `{ name, project_id? }`;手动独立牌组 project_id=null | ✓ |
| PATCH | `/v1/decks/{deck_id}` | 牌组改名;`version` 递增供缓存刷新;返回含真实进度 | ✓ |
| GET | `/v1/decks/{deck_id}` | 详情 + 进度(card_count / due_count / mastered / review_count / mastery_ratio) | - |
| GET | `/v1/decks/{deck_id}/deletion-preflight` | 删除预检:影响范围、阻塞任务与可执行动作 | - |
| DELETE | `/v1/decks/{deck_id}?abandon_pre_generation_tasks=true\|false` | 可原子放弃正式生成前任务;正式生成中仍保护 | ✓ |
| GET | `/v1/decks/{deck_id}/cards` | 自由刷题:支持 order、content_difficulty、mastery 过滤 | - |
| POST | `/v1/decks/{deck_id}/cards` | 手动新增卡片 `{ front, back }`,分配 position | ✓ |
| POST | `/v1/decks/{deck_id}/cards/import` | 批量导入 `{ cards: [{ front, back }] }`,原子写入,返回逐张结果 | ✓ |
| PATCH | `/v1/cards/{card_id}` | 编辑卡片(内容覆盖 + ReviewState 重置为新卡;`version` 递增) | ✓ |
| DELETE | `/v1/cards/{card_id}?delete_batch_id=` | 返回 CardDeletionBatch,不立即硬删;10 秒可撤销 | ✓ |
| GET | `/v1/card-deletion-batches/pending` | App 重启恢复仍有效的撤销批次 | - |
| POST | `/v1/card-deletion-batches/{delete_batch_id}/undo` | 恢复整批 | ✓ |
| POST | `/v1/cards/{card_id}/rewrite-previews` | 生成并持久化重写预览,不改原卡 | ✓ |
| POST | `/v1/cards/{card_id}/rewrite-previews/{rewrite_id}/apply` | 版本一致时原子替换(CAS) | ✓ |
| DELETE | `/v1/cards/{card_id}/rewrite-previews/{rewrite_id}` | 取消预览,可幂等 | ✓ |

自由刷题筛选:`order=position|random`;`content_difficulty=BASIC|UNDERSTANDING|DEEP_QUESTION|UNLABELED`;`mastery=all|mastered|unmastered`。随机顺序由客户端会话 seed 固定,服务端不得每翻一张重新洗牌。自由刷题不创建事件、不改变排程(6.6)。
删除规则:删除后 `tasks.deck_id` 置空,历史任务保留。列表接口 MVP 暂不分页:单牌组卡片量超 1000 张后再引入分页。
导入规则:客户端负责文本解析与预览编辑;服务端仅接收最终确认列表;失败整体回滚(`422 IMPORT_PARSE_ERROR`),不产生部分成功。

### 6.6 学习、复习(V2.5 目标 4.5)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/study/today` | 当前项目今日计划(服务端账号时区、去重;3.20) | - |
| GET | `/v1/decks/{deck_id}/review` | 独立牌组或指定牌组到期复习队列(due <= now,按 due、position 排序) | - |
| POST | `/v1/review-events` | 提交评级 `{ card_id, rating, client_event_id }`(不再要求 device_timezone),返回更新后的 ReviewState 与本次学习日期 | ✓(client_event_id) |

### 6.7 数据看板(V2.5 目标 4.5)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/stats/dashboard` | 当前自然周看板,返回 StatsDashboard;**无 timezone/weekly_goal 查询参数**(服务端按账号偏好派生) | - |

### 6.8 质量观测(FR-10/11,AC-07;审核修复)

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/tasks/{task_id}/batches` | 返回批次列表(Batch),含单卡 Rubric 汇总、质量分布与 Prompt Cache 记录,供测试与联调核验(AC-07) | - |

MVP 无可视化后台;观测数据经此接口 + 卡片详情(Rubric 单卡字段,3.9)核验。

### 6.9 质量聚合观测（O-4，审核设计补全）

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| GET | `/v1/observability/quality-summary?group_by=model\|pdf\|difficulty&days=30` | 跨任务质量聚合:Rubric 各维平均分、覆盖/重复率均值、任务完成率、成本汇总;按 group_by 分组 | - |

- 隔离口径：按当前 `user_id` 聚合（与业务数据同隔离）；跨用户聚合留给未来运营后台。
- **分组键定义**：`model` = `Batch.model`；`pdf` = 任务所属 `file_id`；`difficulty` = `Batch.generation_unit_id` → 对应单元的 `target_difficulty`（**不能只依赖 Card**，否则生成失败、没有 Card 的 coverage=0 批次丢失；单元缺失/锚定缺失 → `unknown`）。聚合结果按 `rubric_version` 拆子组——查询窗口同时包含多版本时不得无标识混算。
- **评分样本口径**：各评分维度只以对应字段非 NULL 的卡为分母（NULL 不计 0 分、不进分母）；`eligible_card_count` = 经批次归属的卡数，`scored_card_count` = `rubric_total_score` 非 NULL 的卡数，`sampling_rate` = `scored/eligible`（分母 0 时返回 null）；覆盖/重复率均值含 SKIPPED 批次（coverage=0 计入分母）。
- 成本汇总（O-6）：按"价格配置常量"换算 `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens` 为估算金额，hit/miss/output 分开计价；价格常量取 DeepSeek 官方定价、标注生效日期，不固化进 DB。**成本口径：`cost_estimate` 为 generation-stage-only**（Batch token 列为生成阶段兼容投影），不得冒充全链路成本；全链路成本按 `llm_call_attempts` 账本分 stage 汇总，禁止把 Batch 投影再次相加造成双计。
- `/healthz`（存活）、`/readyz`（就绪:DB 连接 + 存储可写，失败 503）、`/metrics`（Prometheus 文本）、`/openapi.json`（接口文档，前端对接在线拉取）为运行观测基础端点，**豁免 Bearer 鉴权**（探针/采集器无账号上下文）；`POST /auth/register`、`POST /auth/login` 同样豁免（6.11）。

### 6.10 账号（FR-19,决策 D-05;V2.2 新增;V2.5 增量）

| 方法 | 路径 | 说明 | 幂等 |
| --- | --- | --- | --- |
| POST | `/v1/auth/register` | 创建用户并建立会话;`{ username, email, password }`;201 返回 AuthSessionResponse(3.15);email 冲突 → `409 EMAIL_TAKEN` | - |
| POST | `/v1/auth/login` | 校验凭据并建立新会话;`{ email, password }`;200 返回 AuthSessionResponse;失败统一 `401 INVALID_CREDENTIALS`(邮箱或密码错误) | - |
| POST | `/v1/auth/logout` | 撤销当前会话(仅当前);204 | 幂等键(并发双发单副作用) |
| GET | `/v1/auth/me` | 返回当前用户资料 AuthUser(3.14,含 email/avatar_key);PATCH 见 6.1 | - |

规则:register/login 豁免 Bearer 鉴权与 Idempotency-Key;logout/me 需要 Bearer。客户端不得自动重试
register/login(防网络重放静默创建多条会话)。受保护接口 401(`AUTH_REQUIRED` / `AUTH_INVALID`)携带
`WWW-Authenticate: Bearer`(1.4)。logout 的顺序重放(撤销后同键重试)因撤销 token 统一 401 在认证
闸门先行不可达,幂等键保证并发双发单副作用(条件更新天然幂等);login 的非法格式邮箱按输入
校验惯例返回 400 VALIDATION_ERROR(非 401),不泄露账号存在性。
认证语义(凭据/会话/限流)见 1.1 / 1.6;数据归属与隔离见 1.1 归属声明与 1.3 幂等约定。

## 7. 错误码表

| 分组 | 错误码 | HTTP | 说明 |
| --- | --- | --- | --- |
| 通用 | `VALIDATION_ERROR` | 400 | 请求结构/字段非法 |
| | `RATE_LIMITED` | 429 | 限流(1.6),响应携带 `Retry-After` |
| | `IDEMPOTENCY_CONFLICT` | 409 | 幂等键相同但请求体与首次不一致 |
| | `INTERNAL_ERROR` | 500 | 未预期错误(内部细节仅进日志) |
| 账号 | `AUTH_REQUIRED` | 401 | 缺失 Authorization Bearer 凭据;401 带 `WWW-Authenticate: Bearer` |
| | `AUTH_INVALID` | 401 | token 非法/未知/撤销/过期;401 带 `WWW-Authenticate: Bearer` |
| | `INVALID_CREDENTIALS` | 401 | 登录失败(邮箱不存在与密码错误统一返回,不暴露账号存在性) |
| | `EMAIL_TAKEN` | 409 | 注册邮箱已被占用 |
| 偏好 | `INVALID_PREFERENCES` | 400 | V2.5 比例或每日目标非法(比例 10% 档 0~100 合计 100;目标 10~200 的 10 倍数) |
| | `INVALID_LEARNING_TIMEZONE` | 400 | V2.5 非法 IANA 时区 |
| PDF/项目 | `PDF_UPLOAD_INVALID` | 400 | 非 PDF / 损坏 / 超限(100MB / 1000 页) |
| | `PDF_PARSE_FAILED` | 422 | 文本层解析失败 |
| | `PDF_TOC_MISSING` | 422 | 无可用目录结构(终止流程) |
| | `PDF_NOT_FOUND` | 404 | 不存在或非本用户(统一 404,不暴露存在性) |
| | `CHAPTER_NOT_FOUND` | 404 | 章节不存在或非本文件/本用户(统一 404) |
| | `PROJECT_NOT_FOUND` | 404 | V2.5 项目不存在或跨用户 |
| | `PROJECT_STATE_CONFLICT` | 409 | V2.5 当前项目状态不允许操作 |
| | `PROJECT_HAS_ACTIVE_TASK` | 409 | V2.5 删除被活跃任务阻止 |
| API Key | `API_KEY_UNAVAILABLE` | 502 | Key 缺失/解密失败、chat 上游 401/429/5xx 或校验链路(validate_key)上游不可用(含网络);生成链路中 401(Key 错误)不可重试 → 任务 `FAILED`,429/5xx 可重试(账本预算内);生成链路网络/超时与响应解析失败内部记 `GENERATION_FAILED`(重试预算同) |
| | `API_KEY_NOT_SET` | 422 | 样卡 / 任务启动时未保存 Key |
| 任务 | `TASK_NOT_FOUND` | 404 | |
| | `TASK_STATE_CONFLICT` | 409 | V2.5 非法状态转移(如 abandon/start/retry 前置状态不符) |
| | `TASK_ZERO_CARDS` | 422 | V2.5 正式生成无有效卡(整体失败,不显示"完成 0 张") |
| | `SAMPLE_STALE` | 409 | V2.5 配置变化后仍尝试确认旧样卡 |
| | `TASK_IN_PROGRESS` | 409 | 兼容保留:资源被非终态任务引用(旧 `/pdfs` 委托路径);项目域使用 `PROJECT_HAS_ACTIVE_TASK` |
| | `GENERATION_FAILED` | 500 | 系统级生成失败(任务 FAILED);批次级失败不产生错误响应 |
| 牌组/卡片 | `DECK_NOT_FOUND` | 404 | 不存在或非本用户(统一 404,不暴露存在性) |
| | `CARD_NOT_FOUND` | 404 | |
| | `GENERATION_ITEM_CONFLICT` | 409 | `generation_item_id` 已对应其他卡 |
| | `IMPORT_PARSE_ERROR` | 422 | 导入内容非法(逐行错误随响应返回;客户端预览阶段已拦截为主) |
| | `CARD_DELETE_WINDOW_EXPIRED` | 409 | V2.5 撤销窗口已过 |
| | `CARD_REWRITE_UNAVAILABLE` | 409 | V2.5 来源已失效或非生成卡,不可创建重写预览 |
| | `CARD_VERSION_CONFLICT` | 409 | V2.5 重写预览基于旧版本(apply 时 CAS 失败) |
| | `REWRITE_SCHEMA_INVALID` | 422 | 重写预览的新版本未通过 Schema 校验(原卡保留) |
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
| 1.1 数据主体 | V2.2 FR-19 / AC-12、D-05；V2.3 D-06 撤销 | 一致(V2.2 账号会话取代 D-02；V2.3 设备架构彻底清除) |
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
| 1.1 归属声明(设备架构清除) / database-design 表结构 | V2.3(决策翻转 D-06→V2.3) | 一致(清理收尾与补做验证工作包契约同步) |
| 3.14/3.15 账号与会话 / 6.11 账号接口 / 1.6 限流 / 7 账号错误码 | V2.4(email 登录键 / username 展示名 / 滑动续期) | 一致(email 登录与长期登录工作包契约同步) |
| 1.2 学习时区 / 3.15 偏好 / 3.16 项目 / 3.17 项目设置 / 3.18 删除批次 / 3.19 重写预览 / 3.20 今日计划 / 3.4~3.9 增量 / 4.1 七态 / 6.1~6.7 接口 / 7 新错误码 | V2.5 总 PRD 及模块 PRD(V25-D-01~D-25) | 一致(非视觉计划 NV-01 原子转正) |
