# 离线数据层契约（Android 客户端，offline-foundation-v1）

机器接口权威是 `../Architecture/openapi.yaml`，行为契约权威是 `../Architecture/structure-contract.md`；本文记录客户端本地数据层（`shanka-v25.db` 投影、评分 outbox、请求调度）与服务器权威的关系，冲突时以上述契约为准。

## 1. 权威边界（不变式）

- 服务器始终是 FSRS 排程、学习日期、统计与跨设备最终状态的唯一权威；客户端不实现第二套调度器，不本地推算 due/掌握度/看板指标。
- 离线范围（只读 + 排队）：已同步数据可离线读取；复习评分离线入队、联网补传。PDF 上传、AI 生成、删除、设置修改、普通卡片编辑仍为 online-only，不进入通用离线写队列。
- 评分的本地确认语义：划卡先持久化到 outbox 并立即进入下一张（本地事务成功即推进），服务器 ReviewState 在补传成功后落地——离线期间 UI 显示的下一张来自本地队列投影，不代表服务器已排程。

## 2. 网络栈（单一）

- 全 App 一套 `NetworkStack`（Retrofit + OkHttp + Kotlin Serialization）：单一连接池/dispatcher、统一 Bearer 会话、429 `Retry-After` 单次自动重试（重放同一请求对象，**不重新生成 Idempotency-Key**）、仅元数据的脱敏证据日志（不含 token/API Key/PDF 正文/卡片正反面/完整响应体）。
- 写请求幂等键由调用方创建并持有；上传走共享栈的 60s 读超时 client（multipart）。
- 旧手写 `HttpURLConnection/org.json` V2.5 传输已删除，不存在双网络栈。`SessionStore` 内 org.json 仅用于 Keystore 加密会话的本地序列化，非网络路径。

## 3. Room 投影 `shanka-v25.db`

- 全部业务表带 `user_id` 列（物理查询隔离，不跨账号展示）；服务器 UUID 以 String 保存；schema export + 显式 migration，禁止 `fallbackToDestructiveMigration`。
- 表：`projects`、`project_files`、`project_chapters`、`decks`、`cards`、`review_states`（服务器快照）、`review_queue`、`study_plan`、`today_plan`（主键 `user_id + study_date`）、`today_plan_cards`、`project_progress`、`dashboard_snapshot`、`cache_metadata`、`review_outbox`。
- 列表刷新在事务内替换所属范围（delete+insert 同 scope）；网络失败不触碰写路径，最后成功缓存保全。
- 类型化缓存元数据：资源键、服务器版本/updated_at（存在时）、抓取时间、schema 版本；不把未建模的完整 HTTP JSON 当长期列。
- stale-while-revalidate：Room Flow 立即给已有数据，后台按 TTL 刷新——项目/牌组/卡片 5 分钟，今日计划/进度/统计 60 秒；显式用户刷新 FORCE 覆盖。解析等待链路（智能制卡章节页轮询、进页首载）对 `GET /projects/{id}` 使用强制远端读（`forceRefresh`），绕过项目 5 分钟缓存；强制读失败时回退缓存快照，保证离线重进仍可渲染。
- 跨日语义：`today_plan` 按 `user_id + study_date` 保存；无网且只有旧日缓存时返回可判定的 stale/empty 状态，不把旧今日计划伪装成当天权威队列。
- 登出：取消该用户同步与内存订阅；默认保留账号隔离的非敏感缓存。凭据仍由 Keystore SessionStore 负责，不写 Room。

## 4. 评分 outbox

- `review_outbox` 行：userId、cardId、rating、`client_event_id`（主键，首次入队生成后**永不变更**）、`idempotency_key`（唯一索引，同样固定）、createdAt、status、attemptCount、nextAttemptAt、lastErrorCode。
- 划卡原子性：单个 Room 事务内先写 outbox、后从本地队列隐藏卡片；事务失败卡片不离队并返回错误。不等待网络。
- 补传：进程内同步器在线即发 + WorkManager 兜底（每用户 unique work `review-sync/<uid>`、CONNECTED 约束、指数退避）。同卡事件严格按 createdAt 顺序（全局串行实现）。
- 结果分类：2xx/幂等重放 → COMPLETED 并写服务器 ReviewState；网络/429/5xx → 保留退避重试；401 → 暂停等待重新认证；永久 4xx/冲突 → FAILED（可诊断）并触发恰好一次服务器权威刷新。不静默成功、不无限热循环。
- 服务器侧幂等由 `POST /review-events` 双幂等承担（`Idempotency-Key` 键层 + `UNIQUE(user_id, client_event_id)` 兜底），补传重复不重复计数。
- outbox 一轮排空后合并刷新牌组/今日计划/进度/看板各一次；单次评分成功不再触发逐卡多接口 fan-out。

## 5. 请求调度

- 前台写（评分/首载）立即派发，不排在后台刷新之后；后台刷新 lane 并发上限 1、可整体取消、相同资源 GET single-flight（键 `<user>:<resource>`）。
- 无进程级固定请求间隔（原 220ms pacer 已删除）；突发上限由服务端 IP 令牌桶承担（见 `../Architecture/structure-contract.md` 1.6），客户端不为 IP 维度自行排队。

## 6. 解析等待的读侧对账（parse-wait reconcile）

- 解析等待不阻塞 UI：智能制卡章节页不使用全屏等待弹窗，等待态由 ViewModel 轮询器驱动（退避 1s→5s、窗口 3 分钟、连续 5 次失败判为未决），用户可离开页面；轮询每拍均为强制远端读。
- 终态语义：`AWAITING_CHAPTER_CONFIRMATION`/`READY` 结束等待并合并刷新项目列表一次；`PARSE_FAILED` 展示后端 `error_code` 并提供"重新上传 PDF"（复用 FAILED 项目的 `replace-pdf` 契约）；超时/失败给出可重试出口，不存在无终点的等待。
- 读侧对账：回前台（ON_RESUME）对 `status=PARSING` 的项目各做一次强制详情读，把已完成的解析回写投影；登录时入队一次性 + 15 分钟周期的 `ParseSyncWorker`（CONNECTED 约束）作为后台兜底。前台对账为主路径，周期任务仅覆盖 App 退后台场景。
- 边界：PDF 上传本身仍为 online-only（见 §1），不进入离线写队列；本节只约定"解析结果的读侧同步"，不引入上传 outbox。
