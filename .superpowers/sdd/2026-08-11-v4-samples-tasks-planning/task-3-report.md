# Task 3 报告：任务 service（创建/状态机/查询/取消/resume）+ KnowledgePoint 规划

**状态**：完成（TDD 红→绿；8/8 测试通过；ruff format/check、mypy strict 全绿；全量 268 passed 无回归）

**Commit**：`9680473` feat(tasks): 任务创建/状态机/查询/取消/resume + KnowledgePoint 规划（可测口径）

## 规划口径验证（5.4.1 可测口径）

- **每章分块**：chunk_count = 基础 3 × 密度系数（COMPACT=1 / BALANCED=2 / EXTENSIVE=3）；实测 2 章节 → COMPACT=6 ≤ BALANCED=12 ≤ EXTENSIVE=18（`test_planning_compact_le_balanced_le_extensive`）。
- **确定性命名**：`topic` = `f"{ch.name}-知识点{i+1}"`（如"第1章-知识点1"）；`source_chunk_id` = `f"{ch.chapter_id}:chunk{i+1}"` 含 chapter_id；`priority` = i+1；`status` = "PENDING"（`test_planning_knowledge_point_fields` 全字段断言）。
- **R-13 注释**（模块 docstring 内）：真实文本分块（章节文本抽取）V5A 或后续接入，V4 规划结构正确。

## 任务状态机与创建

- **create_task**：校验顺序 = `validate_config`（VALIDATION_ERROR）→ PDF 归属（`PDF_NOT_FOUND` 404，跨 device 实测）→ 牌组归属（`_owned_deck`，DECK_NOT_FOUND）→ 已保存 Key（查 api_keys 表 `status=AVAILABLE` 行存在，**不解密**；无 → `API_KEY_NOT_SET` 422 实测）→ 建 Task（**创建即 RUNNING** + stage=GENERATING + selected_chapters/generation_config JSON 快照）→ 规划知识点同事务落库 → 返回 Task。
  - stage 决策落实：规划在 create_task 内同步完成，PLANNING 中间阶段不可观测，落库值直接 GENERATING（同步规划、异步生成）。
- **get_task**：`_owned_task` 归属校验（device_id 不符/不存在 → `TASK_NOT_FOUND` 404，`test_tasks_get_missing_404` 实测）。
- **cancel_task**：PENDING/RUNNING/PAUSED → CANCELLED（置 ended_at/updated_at）；终态任务保持不变。
- **resume_task**：DB 条件更新抢占（4.1）`WHERE task_id AND status='PAUSED' AND resumable=1` → RUNNING；**rowcount==0 → 409 `TASK_STATE_CONFLICT`**。SQLite rowcount 有效已实测：双 resume 用例（首次成功 → 再 resume 409）。
- **task_view**：selected_chapters/generation_config/cursor 从 JSON 反序列化（dict/list 视图）；`resumable` 转 bool；对 Task 4 executor/handler 消费可用。

## 与 brief 的偏差（均经工具链强制或契约修正，行为语义不变）

1. **carry-forward（V1 教训）**：两测试文件 fixtures 均先落 devices 行（`PRAGMA foreign_keys=ON`，pdf/deck/task/api_keys 全 FK → devices）；brief 原样 fixtures 无 devices 前置会直接 FK 报错。ApiKey 种子在 `_seed_context(with_key=True)`。
2. **规划落库位置（brief 草稿缺陷修正）**：`plan_knowledge_points` 为纯计算返回未持久化对象（不 add 到 session）——brief 的 test_planning.py 无 Task 行，若规划函数落库会触发 `knowledge_points.task_id → tasks` FK 违反；`create_task` 捕获返回值 `session.add_all(kps)` 与 Task **同事务**落库（brief Step 3 草稿忽略了规划返回值，会导致 `test_tasks_create_runs_and_plans` 的 DB 查询为 0 行）。模块 docstring 注明契约：落库由调用方在 Task 行存在后同事务执行。
3. **task_view 公开**：brief Interfaces 声明 `task_view(task) -> dict`，Step 3 草稿写作 `_task_view`——按 Interfaces 采用公开名；mypy strict → `-> dict[str, object]`（对齐 decks/review 视图惯例）。
4. **resume rowcount 的 mypy 修正**：`session.execute(update(...))` 返回 `Result[Any]` 无 rowcount 属性 → `cast(CursorResult[Any], ...)`。
5. **测试追加**：brief Step 2 为 5 个测试，补 `test_tasks_get_missing_404`（get_task 404 属接口清单，否则无覆盖）。
6. **ruff 拦截修正**：F401（service.py 未用 KnowledgePoint 导入、测试未用 func/select/Deck 导入）；SIM117（4 处嵌套 with → 单 with 多上下文）。
7. **mypy strict 修正**：`_seed_context -> dict[str, Any]`（`dict[str, str | list[str]]` 的键索引访问与 create_task 形参类型不兼容）；`_config() -> dict[str, str | dict[str, float]]`。
8. **`_owned_deck` 跨包导入保持草稿**：`from services.decks.service import _owned as _owned_deck`（同一 service 层私有名导入；未改 decks/service.py——提交范围外，public 化留给后续）。

## 红线遵守

- 红线 4：create_task 只查 `status=AVAILABLE` 行存在，不解密、不接触明文；无 Key 只出 `API_KEY_NOT_SET`。
- 红线 3：错误码全部走 `app.errors.AppError/ErrorCode` 统一注册表（API_KEY_NOT_SET/PDF_NOT_FOUND/TASK_NOT_FOUND/TASK_STATE_CONFLICT 均已存在）。
- 分层：services/tasks → services/generation / services/decks / infra.db / app.errors，无反向依赖；无 handler 暴露 ORM。

## 接口交付（Task 4 executor/handler 消费）

- `services.generation.planning.plan_knowledge_points(session, *, task_id, chapter_ids, quantity_tendency) -> list[KnowledgePoint]`（纯计算，调用方负责落库）。
- `services.tasks.service.create_task(session, *, device_id, file_id, deck_id, chapter_ids, config, now) -> Task`（校验→RUNNING+快照→规划同事务）。
- `services.tasks.service.get_task(session, *, device_id, task_id) -> Task`（404）；`cancel_task(session, *, device_id, task_id, now) -> Task`；`resume_task(session, *, device_id, task_id, now) -> Task`（409）。
- `services.tasks.service.task_view(task) -> dict[str, object]`。
- 新增 `services/tasks/__init__.py`（任务用例包）。

---

## Fix Round 1（评审修正，二次提交）

**状态**：完成（9/9 测试通过；ruff format/check、mypy strict 全绿；全量 269 passed 无回归）

### I-1 [Important] selected_chapters 快照对象化（Chapter[]）

- 契约依据：structure-contract 3.4 `selected_chapters` 类型为 `Chapter[]`（对象数组）；3.6 章节删除后 `knowledge_points.chapter_id` 置 null，名称从 `tasks.selected_chapters` 快照还原——ID-only 快照无法还原名称。
- 修复：`create_task` 从 Chapter 表读取选定章节真实字段，快照存 `[{"chapter_id", "name", "start_page", "end_page"}]`（按入参 chapter_ids 顺序），JSON 序列化入 `selected_chapters`；`task_view` 反序列化后自然返回对象数组（无额外改动）。
- 补测试：`test_tasks_create_runs_and_plans` 新增断言——新 session 读 DB 行 `json.loads(row.selected_chapters) == ctx["chapters"]`（含 name/start_page/end_page 全字段，`_seed_context` 现返回 chapters 快照）。

### M-1 [Minor] chapter_ids 归属校验

- 修复：`create_task` 校验所有 chapter_ids 均属于 file_id（`select(Chapter).where(chapter_id.in_(...), file_id == file_id)`，缺失/他属 → `PDF_NOT_FOUND` 404——与 samples.py 一致，且比 samples 更严：按 file_id 限定查询，他 PDF 章节同样拦截）。
- 补测试：`test_tasks_create_foreign_chapter_404`——同 device 下第二 PDF 的章节混入 chapter_ids → `PDF_NOT_FOUND`。

### 工具链

- mypy strict：无新增类型问题（chapter_snapshot 推断为 `dict[str, object]` 列表）。
- 校验顺序：validate_config → PDF 归属 → 章节归属 → 牌组归属 → Key 校验（章节归属紧跟 PDF 归属，内容校验前置）。

**Commit（fix round 1）**：`fix(tasks): selected_chapters 快照对象化（Chapter[] 名称可还原）+ 章节归属校验（V4-T3 fix round 1）`（具体 hash 见主返回消息）
