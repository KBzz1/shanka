# 后端对接说明（闪卡 App V2.5）

给前端开发者的后端接入指南。**机器可读接口权威**：[openapi.yaml](../Architecture/openapi.yaml)（路径、请求/响应结构）；**行为契约权威**：[structure-contract.md](../Architecture/structure-contract.md)（状态机、幂等、FSRS 排程、错误码）。本文件是这两者的使用导览 + 部署环境信息，若与契约冲突以契约为准。

V2.5 相对旧版的核心变化：数据主体从设备改为**账号**；制卡入口从独立 PDF/样卡/任务三接口收敛为**学习项目 + 七态任务**；新增账号偏好、今日学习计划、删除批次撤销与 AI 重写预览；任务用户侧**无暂停/取消**（改为放弃/重试）；路径**无 `/v1` 前缀**。

---

## 1. 环境信息

| 环境 | Base URL | 说明 |
| --- | --- | --- |
| 生产 | `https://shanka.kbzz1.top` | Cloudflare Tunnel 公网入口（自动 HTTPS）；实测移动网络延迟约 306ms，首连含 TLS 握手 1-3s 属正常 |
| 本地联调 | `http://localhost:8000`（WSL2 内）/ `http://10.0.2.2:8000`（Android 模拟器） | 后端在 WSL2 中运行，启动方式：`/home/kbzz1/shanka_backend/scripts/run.sh`；8000 被**其他程序**占用时脚本自动换 8001（需同步 Cloudflare Tunnel 回源端口），本应用已在运行时幂等退出不重复启动 |

部署架构（生产）：

```text
Android App ──HTTPS──▶ shanka.kbzz1.top（Cloudflare 边缘，TLS）
                            │  Tunnel（出站长连接，无公网开放端口）
                            ▼
                    FastAPI（WSL2，端口 8000）
```

- **路径无 `/v1` 前缀**：契约与实现一致，均为 `/decks`、`/projects/{project_id}/tasks` 等无前缀路径。
- 全部接口**必须走 HTTPS**（契约 1.7）。
- 探活：`GET /healthz`（存活）、`GET /readyz`（DB + 存储就绪，失败 503），豁免鉴权，可用于 App 的启动连通性检查。
- 前端本地联调环境、Debug 包后端地址与真机调试见 [local-dev.md](local-dev.md)；客户端离线数据层（Room 投影 + 评分 outbox）契约见 [offline-data-layer.md](offline-data-layer.md)。

## 2. 全局请求规范（每条请求都要遵守）

### 2.1 请求头

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <token>` | 所有业务接口（除 /auth/register、/auth/login、/healthz、/readyz、/metrics、/openapi.json） | 注册/登录后获得的 opaque session token；token 只保存在客户端安全存储 |
| `Idempotency-Key` | 所有写操作 | 客户端生成 **UUID v4** |

**token 等同于密码**：泄漏后在被撤销或过期前可被冒用。不要写日志、不要展示；受保护接口 401 时按 `WWW-Authenticate: Bearer` + `localization_key` 回登录页（离线网络失败不得误判为会话失效）。

### 2.2 幂等语义（写操作）

- 同一 `(用户, 接口, Idempotency-Key)` 的重复请求 → 服务端**返回首次成功结果**（同状态码 + 同响应体），不产生重复数据。
- 键相同但请求体与首次不一致 → `409 IDEMPOTENCY_CONFLICT`（编码错误）。
- **客户端实践**：每次新操作生成新 UUID v4 键；网络重试必须**复用同一键**（否则可能双写）。离线补传同理。
- 复习评级（`POST /review-events`）额外支持 `client_event_id` 兜底幂等（见 5.3）。

### 2.3 时间与时区

- 所有时间字段：**ISO 8601 UTC**（RFC 3339），例 `2026-08-10T09:00:00Z`；显示时客户端自行转本地时区。
- 服务端时间是权威时钟：到期判断（`due`）、统计分桶都用服务端时间。
- **学习时区由账号偏好携带**（`PATCH /preferences` 的 IANA 时区，如 `Asia/Shanghai`），今日计划分桶、复习 `study_date`、周统计均按它计算——客户端不再逐请求上报时区。

### 2.4 错误响应（统一结构）

```json
{
  "error": {
    "code": "DECK_NOT_FOUND",
    "message": "人类可读的补充信息",
    "localization_key": "error.deck_not_found"
  }
}
```

- 客户端按 **`localization_key`** 映射本地文案（`error.` + 错误码 snake_case；错误码稳定，消息文本可能变化）。
- 跨账号访问他人资源一律 `404`（不暴露存在性）——遇到 404 不要猜测资源是"不存在"还是"不属于你"。

### 2.5 限流（客户端不得硬编码阈值，超限重试即可）

| 维度 | 默认阈值 | 覆盖接口 |
| --- | --- | --- |
| 写操作 | 60 次/分钟/用户 | 全部写接口 |
| IP | 持续 5 次/秒 + 突发 10（令牌桶） | 全部接口；同 IP 首个短突发（如启动刷新）可立即放行 10 个，耗尽后按 1 个/0.2s 恢复 |
| 注册/登录 | 按来源 IP 与规范化邮箱分桶（运维可调） | 防分布式猜测 |
| PUT /api-key | 10 次/小时/用户 | |
| 样卡生成 `POST /tasks/{task_id}/samples` | 20 次/小时/用户 | 样卡消耗模型配额 |
| PDF 资料上传（`materials/pdf`） | 10 次/小时/用户 | |

超限 → `429 RATE_LIMITED` + `Retry-After` 响应头（按头里秒数等待后重试；服务端按下一个令牌可用时间向上取整，最少 1 秒）。客户端离线补传串行发送 + 指数退避即可，无需为 IP 维度自行排队。

## 3. 接口总览（按业务流程分组）

> 每个接口的请求/响应字段定义以 openapi.yaml 为准，以下为用途导览。

### 3.1 账号（注册 / 登录 / 会话）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/register` | 邮箱注册（重复 → `409 EMAIL_TAKEN`） |
| POST | `/auth/login` | 邮箱登录（失败 → `401 INVALID_CREDENTIALS`，不区分具体原因） |
| POST | `/auth/logout` | 撤销当前会话 |
| GET / PATCH | `/auth/me` | 读取 / 更新当前账号资料（如 avatar_key） |

- 数据主体 = 账号：换设备登录同一账号即可看到同一份数据；离线期间的本地变更经 outbox 补传（见 5.4）。

### 3.2 账号偏好

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/preferences` | 覆盖深度、整数难度比例、每日目标、学习时区（IANA）、当前项目 |
| PATCH | `/preferences` | 部分更新；比例、目标、IANA 时区服务端校验（`400 INVALID_PREFERENCES` / `INVALID_LEARNING_TIMEZONE`） |

- 制卡页的默认生成配置来源于此；学习时区驱动今日计划与统计分桶。

### 3.3 学习项目、资料与章节（V2.5 制卡入口，多资料）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects` | **JSON `{name}` 建立空项目**（两步创建第一步；资料经 materials 端点添加） |
| GET | `/projects` | 当前用户项目列表（真实空态） |
| GET | `/projects/{project_id}` | 项目详情 + 资料集合状态 + 跨资料章节列表；任一资料 `PARSING` 时轮询 |
| PATCH | `/projects/{project_id}` | 更新项目名 |
| DELETE | `/projects/{project_id}` | 删除项目；活跃任务由服务端同事务取消，预检接口见下 |
| GET | `/projects/{project_id}/deletion-preflight` | 删除预检（只读诊断，不是资源锁） |
| POST | `/projects/{project_id}/materials/pdf` | 添加 PDF 资料（multipart 字段 `file`，≤100MB、≤1000 页；异步解析；重置章节确认） |
| POST | `/projects/{project_id}/materials/text` | 添加粘贴文本资料（`{name, content}`，≤30000 字；单章节即时就绪） |
| GET | `/projects/{project_id}/materials` | 资料列表（各自状态；TEXT 附单章节） |
| DELETE | `/projects/{project_id}/materials/{material_id}?retain_cards=` | 资料级删除：引用任务服务端静默取消；`retain_cards` 选择保留或一并删除该资料产出卡片 |
| GET | `/projects/{project_id}/materials/{material_id}/deletion-preflight` | **资料删除确认页预检**：返回将影响的卡片数量与静默取消任务数（PRD V25-GEN-FR-02） |
| POST | `/projects/{project_id}/materials/{material_id}/replace` | 仅 `FAILED` 的 PDF 资料原位替换并重新解析（不影响其他资料） |
| GET | `/projects/{project_id}/progress` | 项目进度投影（card_count / 各状态计数 / due_count 等） |
| GET | `/projects/{project_id}/stats/weekly` | 项目周统计 |
| PATCH / DELETE | `/projects/{project_id}/chapters/{chapter_id}` | 修改章节名称/起止页（TEXT 章节仅名称）；删除章节（保留卡时 chapter_id 置空进"未归属章节"） |
| POST | `/projects/{project_id}/confirm-chapters` | 确认目录，项目进入 READY |
| GET / PATCH | `/projects/{project_id}/study-settings` | 项目级学习设置 |
| POST | `/projects/{project_id}/decks/{deck_id}/attach` | 将已有牌组挂到项目 |

- **项目是资料集合**（V25-D-29）：可同时含 PDF 与文本资料；项目状态由全部资料聚合（`EMPTY` → `PARSING`/`AWAITING_CHAPTER_CONFIRMATION` → `READY`，全 PDF 失败 → `PARSE_FAILED`）；新增/删除任一资料都会重置章节确认。
- `Material.status`（PDF）：`PENDING` → `PARSING` → `PARSED` / `FAILED`；`FAILED` + `error_code`（`PDF_PARSE_FAILED` 文本层失败 / `PDF_TOC_MISSING` 无目录）——**前端应终止流程**，没有 AI/OCR 兜底，仅支持原位替换。扫描版 PDF 无文本层属预期边界，如需支持应作为 OCR 能力另行排期。
- 旧 `/pdfs` 兼容路径已随多资料改造移除（structure-contract 6.2）；一律走 `/projects` 系列。

### 3.4 API Key——DeepSeek 密钥

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api-key` | 验证并保存；响应仅含 `status` + 脱敏标识 `sk-****abcd` |
| GET | `/api-key/status` | 查询状态；未保存时 `200` + `status=UNKNOWN`、`masked_key=""` |

- 校验结果一律经 `200 + ApiKey.status` 返回：`AVAILABLE` / `INVALID` / `INSUFFICIENT_BALANCE` / `UNKNOWN`（不产生 4xx 错误响应）。
- **`INVALID` 校验结果不覆盖已保存的有效 Key**（防冒用者替换他人有效 Key）；只有 `AVAILABLE` 才覆盖旧 Key。因此用户输入错误 Key 后查询状态可能仍是 `AVAILABLE`（旧 Key 仍有效）——UI 提示"密钥无效"即可，不要引导重存。
- 客户端**不得持久化 Key 明文**、UI 不展示完整 Key；Key 只经 HTTPS 上传，任何日志/响应/任务明细不得出现明文。

### 3.5 制卡任务（V2.5 七态）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/projects/{project_id}/tasks` | 建立 **DRAFT** 任务（自动保存语义；保存章节快照、目标牌组、生成配置） |
| GET | `/tasks` | 学习页任务区与历史列表（支持 `project_id` / `status` 过滤） |
| GET | `/tasks/{task_id}` | 任务详情（七态、`internal_stage`、样卡、失败码）；长任务轮询（见 5.2） |
| PATCH | `/tasks/{task_id}` | 仅 `DRAFT` / `AWAITING_SAMPLE_CONFIRMATION` 可改配置；修改后样卡失效 |
| DELETE | `/tasks/{task_id}` | 删除终态任务（`delete_generated_cards` 选择是否删除其已发布卡） |
| POST | `/tasks/{task_id}/samples` | 持久化生成 1~3 张样卡（只为比例大于 0 的难度各 1 张；幂等键防重复触发） |
| POST | `/tasks/{task_id}/start` | 校验样卡 hash 后进入 `GENERATING`（过期样卡 → `409 SAMPLE_STALE`） |
| POST | `/tasks/{task_id}/abandon` | 放弃任务（仅正式生成前状态，进入 `ABANDONED` 终态） |
| POST | `/tasks/{task_id}/retry` | 失败任务创建关联新任务（`retry_of_task_id` 指向原任务；正式生成失败可沿用已确认样卡） |
| GET | `/tasks/{task_id}/batches` | 批次列表（联调/质量核验用，含 Rubric、token 用量、成本估算） |

创建任务请求体（请求中**不携带 API Key**，服务端使用已保存 Key；`project_id` 取自路径）：

```json
{
  "deck_id": "<uuid>",
  "chapter_ids": ["<uuid>"],
  "generation_config": { /* 同下 */ }
}
```

**生成配置 `generation_config`**（样卡与正式生成共用）：

```json
{
  "coverage_mode": "BALANCED",
  "difficulty_ratio": { "basic": 40, "understanding": 40, "deep_question": 20 },
  "custom_requirements": "多给公式推导步骤"
}
```

- `coverage_mode`：`COMPACT`（精简）/ `BALANCED`（均衡）/ `EXTENSIVE`（充分覆盖）——语义覆盖范围，不承诺数量。
- `difficulty_ratio`：三个键必填、10% 整数档、**允许 0 但不可全 0**（全 0 创建/修改时即拒绝）；难度枚举为 `BASIC`（基础记忆）/ `UNDERSTANDING`（理解分析）/ `DEEP_QUESTION`（深度提问）。
- 配置**不继承**：新任务由服务端从账号偏好取默认，任务内修改只影响本任务；改配置后样卡失效需重新生成。

### 3.6 牌组、卡片与删除撤销

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/decks` | 牌组列表（含进度摘要；支持 `project_id` 归属过滤） |
| POST | `/decks` | 新建 `{ name }` |
| GET / PATCH / DELETE | `/decks/{deck_id}` | 详情+进度 / 改名（`version` 递增）/ 删除（级联卡片与复习状态；活跃任务服务端同事务取消） |
| GET | `/decks/{deck_id}/deletion-preflight` | 删除预检（只读诊断） |
| POST | `/decks/{deck_id}/cards` | 手动新增 `{ front, back }` |
| POST | `/decks/{deck_id}/cards/import` | 批量导入 `{ cards: [{front, back}] }`，原子写入，返回逐张结果 |
| GET | `/decks/{deck_id}/cards` | 自由刷题：全部卡片（不创建事件、不改排程） |
| PATCH | `/cards/{card_id}` | 编辑 `{ front, back }`；**内容覆盖 + 复习状态重置为新卡**；`version` 递增 |
| DELETE | `/cards/{card_id}` | 删除单卡（进入可撤销的删除批次，见下） |
| GET | `/card-deletion-batches/pending` | 待撤销删除批次列表（10 秒服务端撤销窗口） |
| POST | `/card-deletion-batches/{delete_batch_id}/undo` | 撤销删除批次（卡片与复习状态恢复） |

- 批量导入**原子写入**：任一卡片校验失败 → 整体回滚并返回 `422 IMPORT_PARSE_ERROR`（不会部分成功）；全部成功 → `201` + `results: [{ index, status, card_id }]` 逐条结果。
- `Deck.version` / `Card.version` 为变更版本（更新/重写时递增），供客户端本地缓存刷新判断；`Deck.source` / `Card.source`（`MANUAL` / `IMPORTED` / `GENERATED`）区分来源展示。
- 卡片渲染：**`front` / `back` 为通用渲染字段（所有卡片必有）**；判断题（`card_type=TRUE_FALSE`）另有结构化字段（`statement` / `answer_boolean` / `explanation`），可直接用 `front`/`back` 渲染，结构化字段留待判断题专用视图。
- 生成卡附带 Rubric 质量分（各维 0~3、`rubric_total_score` 0~12）与 `target_difficulty`——联调/质量核验用，普通列表可不展示。
- 单卡删除/批量删除进**删除批次**：客户端先做 10 秒可撤销 UI（对照 `GET /card-deletion-batches/pending`），撤销窗口过后级联清理生效。

### 3.7 单卡 AI 重写（预览制）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cards/{card_id}/rewrite-previews` | 生成重写预览（两阶段模型调用；不直接改卡） |
| POST | `/cards/{card_id}/rewrite-previews/{rewrite_id}/apply` | 应用预览：**原地替换**（同一 `card_id`，position 不变），复习状态重置 |
| DELETE | `/cards/{card_id}/rewrite-previews/{rewrite_id}` | 取消/丢弃预览 |

- 应用失败（如新版本未过 Schema 校验 → `422 REWRITE_SCHEMA_INVALID`）时**原卡及原排程保留**；成功后 `updated_at` / `version` 递增。
- 应用后 `front`/`back` 已变化，前端按"内容更新"刷新该卡即可，不要重建卡片条目。

### 3.8 今日学习计划

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/study/plan` | 当前项目今日学习计划配置；未配置时 `configured=false` |
| PUT | `/study/plan` | 原子保存计划（`project_id` + `selected_deck_ids` + `daily_new_goal` / `daily_review_goal`，0~200 且为 10 的倍数） |
| GET | `/study/today` | 当前项目今日计划（服务端按账号学习时区分桶、去重；到期优先 + 新卡补足）；无当前项目时 `current_project` 为 null（空态） |
| GET | `/study/today/backlog` | 超过巩固软目标的到期卡分页（`offset` / `limit`，唯一分页列表接口） |

### 3.9 复习（FSRS-6）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/decks/{deck_id}/review` | 到期卡片队列（`due <= now`，按 due、position 稳定排序） |
| POST | `/review-events` | 提交评级（见 5.3），返回更新后的 ReviewState 与本次学习日期 |

- 队列项 = `Card` + 内嵌 `review_state`（`state` / `due` / `stability` / `difficulty` 等）——每张到期卡自带排程快照，前端直接渲染，无需额外请求。
- 自由刷题（`GET /decks/{deck_id}/cards`）返回裸 `Card`（无 `review_state` 内嵌）。

### 3.10 数据看板

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/stats/dashboard` | 当前自然周看板（周一起始；**服务端按账号学习时区分桶**，周目标=每日目标×7；无客户端参数） |

- 分母为 0 的比率一律返回 `null`（不是 0%）；`has_data=false` 时展示空态。

### 3.11 质量观测（联调/核验用，App 无需实现）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/observability/quality-summary?group_by=model\|pdf\|difficulty&days=30` | 跨任务质量聚合：Rubric 各维平均分、覆盖/重复率、任务完成率、成本估算；按当前用户隔离 |

- `/tasks/{task_id}/batches` 为单任务批次明细。App 不依赖此接口。

## 4. 关键状态机（前端需要理解的部分）

### GenerationTask（V2.5 七态）

```text
POST /projects/{project_id}/tasks → DRAFT（自动保存，创建即返回）
DRAFT ──POST /samples──→ SAMPLE_GENERATING ──成功──→ AWAITING_SAMPLE_CONFIRMATION
DRAFT | SAMPLE_GENERATING | AWAITING_SAMPLE_CONFIRMATION ──PATCH 改配置──→ 样卡清空（回到待生成样卡）
DRAFT | SAMPLE_GENERATING | AWAITING_SAMPLE_CONFIRMATION ──abandon──→ ABANDONED
AWAITING_SAMPLE_CONFIRMATION ──start（校验样卡 hash）──→ GENERATING
GENERATING ──整批成功发布──→ COMPLETED（generated_card_count = 最终发布数）
GENERATING ──任一失败──→ FAILED（零部分可见）
FAILED ──retry──→ 新任务 DRAFT（retry_of_task_id 指向原任务）
```

- **前端映射**：`GENERATING` = 生成中，`COMPLETED` = 完成，`FAILED` = 失败可重试，`ABANDONED` = 已放弃。
- **用户侧无暂停/取消/断点续传接口**：`PAUSED` / `resume` / `cancel` 已随 V2.5 删除；`DRAFT` 自动保存语义下，页面切换、App 退出或换设备后重新读取任务即可继续配置。生成期内部恢复由服务端租约机制完成，不暴露用户状态。
- **零部分可见**：正式生成写入的卡先为 `STAGED`（用户不可见），同一事务内整批发布；任何阶段失败 → 任务 `FAILED`，`STAGED` 卡继续隔离。0 张有效卡整体失败（`TASK_ZERO_CARDS`）。
- `FAILED` 对应系统级不可恢复错误（API Key 失效、上游持续不可用）或 0 张有效卡，响应含 `failure_stage` + `error_code`（如 `API_KEY_NOT_SET`）。批次级失败不置 `FAILED`——该批 `SKIPPED`，任务继续。
- `internal_stage`（`PLANNING → GENERATING → SCORING → PUBLISHING`）仅为运行期观测，不作为用户状态。

### Material（PDF 资料解析；TEXT 恒为 READY）

`PENDING` → `PARSING` → `PARSED`（成功）/ `FAILED`（终态，带 error_code；仅此状态支持原位替换）

### 卡片复习（FSRS-6，服务端计算）

```text
NEW → LEARNING → REVIEW ⇄ RELEARNING
```

客户端**不要自行计算**排程；评级接口返回新 ReviewState，前端直接使用 `due` 字段。四档评级含义：`AGAIN` 忘记 / `HARD` 模糊 / `GOOD` 记得 / `EASY` 简单。

## 5. 关键流程指引

### 5.1 项目制卡主流程（PDF → 生成）

1. `POST /projects`（JSON `{name}`）建立空项目 → `POST /projects/{project_id}/materials/pdf` 上传 PDF 资料（带 Idempotency-Key）→ 观察 `GET /projects/{project_id}`（或项目列表）直到解析 `PARSED`（拿跨资料章节列表）。解析终态会刷新项目 `version`/`updated_at`（契约 4.5），客户端可凭版本变化感知，无需无条件穿透缓存。
2. （可选）`PATCH` 修改章节边界 → `POST .../confirm-chapters` 确认章节。
3. `PUT /api-key` 保存 DeepSeek Key（如未保存；无 Key 时后续任务动作会失败并提示 `API_KEY_NOT_SET`）。
4. `POST /projects/{project_id}/tasks` 建立 DRAFT 任务（自动保存）→ `POST /tasks/{task_id}/samples` 预览样卡（不满可 `PATCH` 改配置后重新生成）。
5. `POST /tasks/{task_id}/start` 确认样卡进入生成 → 按 5.2 轮询至终态；失败可 `retry`。任务进入终态同样刷新所属项目 `version`。
6. `COMPLETED` 后卡片进入目标牌组，可复习。

### 5.2 长任务轮询

- `GET /tasks/{task_id}` 返回 status / `internal_stage` / `generated_card_count` / 批次进度 / `updated_at`。
- 轮询间隔建议 2-3 秒；`updated_at` 用于区分"还在跑"与"卡住"。
- 后台任务在 API 进程内执行（进程内调度器，无外部队列）；服务端重启后由租约/心跳机制重新抢占恢复，客户端只需继续轮询。
- 客户端实现约定（V25-D-34）：对在途资源（解析中项目、非终态任务）的轮询由统一观察引擎承担，结果写入本地 Room 投影，界面从投影流读取——禁止各界面自建轮询循环。

### 5.3 复习评级（幂等重点）

```http
POST /review-events
{ "card_id": "...", "rating": "AGAIN|HARD|GOOD|EASY", "client_event_id": "<UUID v4>" }
```

- **双幂等键**：`Idempotency-Key` 头幂等表命中优先；未命中时以 `client_event_id` 兜底——同 `client_event_id` 且 `card_id` + `rating` 一致 → 返回首次成功结果；不一致 → `409 REVIEW_EVENT_CONFLICT`。
- **`client_event_id`：每次评级新生成、设备内唯一**；离线补传或重试必须复用同一 `client_event_id`（不重复计数）。
- 评级允许对任意状态卡片提交（服务端按 FSRS 正常排程）。
- 响应为更新后的 ReviewState + `study_date`（账号学习时区下的本次学习日期）。

### 5.4 离线优先（评分 outbox）

客户端离线数据层（Room 投影 `shanka-v25.db` + 评分 outbox 恰好一次补传）的完整契约见 [offline-data-layer.md](offline-data-layer.md)。对接要点：复习评级先落本地 outbox 再异步补传，补传复用同一 `client_event_id` 与 `Idempotency-Key`；其余写操作在线直发并遵守 2.2 幂等纪律。

## 6. 错误码速查（前端需处理的子集）

| 错误码 | HTTP | 前端处理 |
| --- | --- | --- |
| `AUTH_REQUIRED` / `AUTH_INVALID` | 401 | 会话缺失/无效/过期：原子清除本地 token 并回登录页（离线网络失败不得误判为退出） |
| `INVALID_CREDENTIALS` | 401 | 登录失败：提示邮箱或密码错误（不区分具体原因） |
| `EMAIL_TAKEN` | 409 | 注册失败：提示更换邮箱 |
| `RATE_LIMITED` | 429 | 按 `Retry-After` 等待后重试 |
| `IDEMPOTENCY_CONFLICT` | 409 | 编码错误（同键不同体），检查重试逻辑 |
| `INVALID_PREFERENCES` / `INVALID_LEARNING_TIMEZONE` | 400 | 偏好校验失败：比例/目标非法、时区非 IANA |
| `PDF_UPLOAD_INVALID` | 400 | 提示文件不合规（非 PDF/损坏/超 100MB/超 1000 页） |
| `PDF_PARSE_FAILED` / `PDF_TOC_MISSING` | 422 | 提示解析失败/无目录，终止流程 |
| `PROJECT_NOT_FOUND` / `MATERIAL_NOT_FOUND` / `PROJECT_STATE_CONFLICT` | 404 / 409 | 项目/资料不存在（含跨用户） / 状态冲突（刷新后重试） |
| `CHAPTER_NOT_FOUND` | 404 | 章节不存在（可能已被删除） |
| `API_KEY_NOT_SET` | 422 | 引导用户先保存 API Key |
| `API_KEY_UNAVAILABLE` | 502 | 上游校验不可用，稍后重试 |
| `TASK_NOT_FOUND` / `TASK_STATE_CONFLICT` | 404 / 409 | 任务不存在 / 状态机不允许该操作（刷新任务后重试） |
| `TASK_ZERO_CARDS` | 422 | 生成结果 0 张有效卡（任务 FAILED 展示） |
| `TASK_IN_PROGRESS` | 409 | 删除保护（有生成中的任务引用该资源） |
| `SAMPLE_STALE` | 409 | 配置与样卡 hash 不一致：重新生成样卡后 start |
| `DECK_NOT_FOUND` / `CARD_NOT_FOUND` / `GENERATION_ITEM_CONFLICT` | 404 / 409 | 按 localization_key 展示 / 编码错误 |
| `IMPORT_PARSE_ERROR` | 422 | 导入内容非法，逐行错误随响应返回（客户端预览阶段拦截为主） |
| `CARD_DELETE_WINDOW_EXPIRED` | 409 | 撤销窗口已过，删除已生效 |
| `CARD_REWRITE_UNAVAILABLE` / `REWRITE_SCHEMA_INVALID` / `CARD_VERSION_CONFLICT` | 422 / 409 | 重写不可用 / 预览未过 Schema（原卡保留）/ 卡片版本冲突（刷新后重试） |
| `REVIEW_EVENT_CONFLICT` | 409 | 同 client_event_id 但内容不一致（检查重试逻辑） |
| `REVIEW_EVENT_INVALID` | 400 | 评级非法（rating 不在四档内等） |
| `GENERATION_FAILED` | 500 | 系统级生成失败（任务 FAILED 时展示） |
| `VALIDATION_ERROR` | 400 | 请求结构/字段非法 |
| `INTERNAL_ERROR` | 500 | 服务端未预期错误，提示稍后重试 |

完整错误码表见 structure-contract.md 第 7 章。

## 7. 注意事项汇总

1. **账号即数据主体**：登录账号决定数据归属；换设备登录同一账号可见同一份数据；跨账号资源访问一律 404。
2. **幂等键纪律**：新操作新键、重试同键；复习评级另有 `client_event_id` 兜底（见 5.3）。
3. **时间**：所有解析用 UTC；显示时自行转本地时区；学习时区在账号偏好中维护，勿逐请求上报。
4. **轮询刷新**：项目 PDF 解析与生成任务都要轮询；用响应里的 `updated_at`/`status` 判断刷新。
5. **列表不分页**：除 `/study/today/backlog`（offset/limit）外全量返回。
6. **`/favicon.ico` 等非业务路径返回 401**：不在 App 请求范围内，忽略即可。
7. **Key 脱敏**：响应里永远只有 `sk-****abcd` 形式，不要期待完整 Key 回传。
8. **本地联调**：模拟器用 `http://10.0.2.2:8000` 访问宿主机 WSL2 后端；真机联调方式见 [local-dev.md](local-dev.md) 第 8 节。
9. **批量导入逐行结果**：`results[].status` 逐条判断，失败项带 `error`；成功项带 `card_id`。
10. **接口来源**：`docs/Architecture/openapi.yaml`（机器契约）+ `docs/Architecture/structure-contract.md`（行为契约）为唯一权威；生产/本地 `/openapi.json` 已豁免鉴权，可在线拉取校验。
