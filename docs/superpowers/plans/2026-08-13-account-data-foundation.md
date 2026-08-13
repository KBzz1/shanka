# 账号数据地基 P3：users/auth_sessions + owner 表 user_id 迁移（无 legacy claim 版）

- 计划日期：2026-08-13
- 上游权威：`docs/account-auth-test-platform-long-run-v1/DESIGN.md` §5.1/§5.2（§5.3 claim 部分不在范围）；
  合并包 `docs/llm-account-long-run-v1/DESIGN.md` §1.2 非目标、§3 全局约束（只读，禁止修改）
- 契约依据：`docs/Architecture/database-design.md` §7.1（P2 已写入 V2.2 目标态规格）、
  `docs/PRD/V2.2/prd_v2_2.md` FR-19 / D-05 / D-06（本计划实施的是其数据地基部分，不改动契约文件本身）
- 当前 Alembic head：`2a391e994f93`（0003_llm_pipeline_upgrade；执行时以 `alembic heads` 实测为准）

## 全局约束（合并红线，违反即失败）

1. 执行环境：`cd /home/kbzz1/shanka_backend/main`；解释器一律
   `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`。
2. 四工具全绿（每任务验收）：`python -m pytest`（全部）、`python -m ruff check .`、
   `python -m ruff format --check .`、`python -m mypy .`（line-length 100、mypy strict）。
3. TDD：先写失败测试并运行确认失败（红色），再实现（绿色）；每任务按提交信息 commit。
4. 红线 2：ORM ↔ `docs/Architecture/database-design.md` 表结构一致——**每个提交内**契约守卫
   `tests/contract/test_orm_database_guard.py` 必须全绿；表结构变更一律经 Alembic 迁移。
5. **不物理删除**旧列、旧表或历史行；不做任何数据搬运或 API Key 密文复制；旧 device_id 行
   原样保留（不迁不删，无访问路径，D-06）。
6. 本计划不改 `docs/PRD/`、`structure-contract.md`、`openapi.yaml`（P2 已同步）；不改 services/app
   业务逻辑（P3 只动 ORM 模型、迁移、database-design.md 与测试）；现有 v2.1 行为（按 device_id
   写入与查询）必须继续工作。
7. 迁移文件名不硬编码 `0004`：`down_revision` 取执行时 `alembic heads` 的实测值（预期 `2a391e994f93`）。
8. 敏感信息不进日志/测试报告/命令参数；不部署、不 push、不迁移生产库。
9. 禁止破坏性 git 命令（reset/restore/clean/stash）；`git add` 只限本任务文件清单；不碰
   `docs/llm-account-long-run-v1/`、`docs/account-auth-test-platform-long-run-v1/`。

## 目标 schema（database-design §7.1 已定稿，逐字权威）

新表：

- `users`：`user_id TEXT PK`、`username TEXT NOT NULL UNIQUE`（存服务端转小写后的规范化值）、
  `password_hash TEXT NOT NULL`、`created_at TEXT NOT NULL`、`updated_at TEXT NOT NULL`。
- `auth_sessions`：`session_id TEXT PK`、`user_id TEXT NOT NULL FK → users ON DELETE CASCADE`、
  `token_hash TEXT NOT NULL UNIQUE`、`created_at TEXT NOT NULL`、`expires_at TEXT NOT NULL`、
  `revoked_at TEXT NULL`；索引 `(user_id)`。

owner 表：

- 直接归属 6 表 `pdf_files / tasks / decks / cards / review_events / llm_call_attempts`：
  新增 `user_id TEXT NULL, FK → users`（旧行 NULL，新写入由应用保证必填）；原 `device_id` 列
  由 `NOT NULL` 降级为 `NULL`（新行不生成 device_id，旧行值保留）；每表加
  `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`；补充查询索引：
  `ix_pdf_files_user_created (user_id, created_at)`、`ix_tasks_user_created (user_id, created_at)`、
  `ix_decks_user_updated (user_id, updated_at)`、`ix_cards_user_deck (user_id, deck_id)`、
  `ix_review_events_user_reviewed (user_id, reviewed_at)`、`ix_llm_call_attempts_user_created (user_id, created_at)`。
  `review_events` 另加 `UNIQUE (user_id, client_event_id)`；原 `UNIQUE (device_id, client_event_id)`
  保留（SQLite 多 NULL 不冲突）。
- `api_keys`：主键由 `device_id` 重建为 `user_id`（batch 操作；`user_id` 为 PK 列允许 NULL，
  旧行 user_id NULL）；`device_id` 降级为遗留 NULL 列（FK → devices 保留）；其余列不变。
- `idempotency_keys`：复合主键重建为 `PRIMARY KEY (user_id, path, idempotency_key)`（batch）；
  `device_id` 降级为遗留 NULL 列；保留遗留唯一约束 `UNIQUE (device_id, path, idempotency_key)`；
  其余列不变。

迁移纪律：

- 重建约束的表（api_keys、idempotency_keys）用 Alembic `batch_alter_table`，显式检查外键/索引/级联。
- `downgrade()` 第一件事做**数据前置检查并 fail closed**：`users` 非空，或任一 owner 表存在
  `user_id IS NOT NULL` 行 → 在任何 DDL/DML 前抛异常拒绝；空库/纯旧数据副本允许正常降级。
- SQLite 特例（写进 ORM 注释）：`api_keys.user_id` 与 `idempotency_keys.user_id` 为
  `primary_key=True, nullable=True`（SQLite rowid 表非 INTEGER 主键允许 NULL，多 NULL 不冲突）。

database-design.md 同步（与 ORM 同批，守卫 2）：

- §2 表定义翻新为 V2.2 目标态：新增 `### 2.15 users`、`### 2.16 auth_sessions`；2.2/2.3/2.5/2.8/2.9/
  2.11/2.12/2.14 各表补 `user_id` 行、`device_id` 改为遗留 NULL 说明；2.12 主键注释行改为
  `` 主键:`PRIMARY KEY (user_id, path, idempotency_key)` ``（守卫按此行校验列序）。
- §0 隔离键声明改为 user_id（删除"实现态/随迁移更新"过渡措辞）；§1 ER 图改为 users 为根、
  devices 标注"仅兼容审计（旧 device_id 行残留，无归属边）"；§2 状态说明行删除；§3 级联
  `cards.device_id 由服务端写入` → `cards.user_id`；§4 看板索引 `(device_id, reviewed_at DESC)` →
  `(user_id, reviewed_at DESC)`；§7.1 重写为落地记录（V2.2 目标态已实现 + 遗留列保留声明）。

## 任务

### Task 1：users/auth_sessions 新表 + 直接归属 6 表 user_id 迁移（ORM + 迁移 + database-design 同批）

**失败测试（先红）**：

1. `tests/integration/test_alembic_migration.py`：
   - `test_alembic_upgrade_creates_all_tables` 期望表集合加 `users`、`auth_sessions`（+ `text_chunks`、
     `llm_call_attempts` 若当前断言缺则一并补齐——以 0003 后真实表集为准）；
   - 新增 `test_alembic_users_auth_sessions_columns`：PRAGMA table_info 断言两新表列集合与约束
     （users.username UNIQUE、auth_sessions.token_hash UNIQUE、auth_sessions.user_id FK）；
   - 新增 `test_alembic_owner_tables_have_user_id`：6 个直接归属表 PRAGMA table_info 含 `user_id`，
     `device_id` 不再 NOT NULL（notnull=0），且 CHECK（双非空）存在（sqlite_master SQL 文本匹配）。
2. `tests/contract/test_orm_database_guard.py`：随 database-design §2 同步自动红→绿（表名/列集/主键
   全等）；无需新写测试，但守卫必须在该任务内转绿。

**实现**：

- 新迁移 revision（`down_revision = <alembic heads 实测值>`，文件名按 Alembic 默认生成，不硬编码 0004）：
  upgrade 建 `users`、`auth_sessions`；6 个直接归属表 `add_column user_id + FK → users`、
  `alter device_id nullable=True`、加 CHECK 与查询索引（命名见目标 schema）；downgrade 反向（本任务
  先不含 fail-closed 预检——该逻辑在 Task 2 与 PK 重建一并落地，本任务 downgrade 只做反向 DDL）。
- `main/infra/db/models.py`：新增 `User`、`AuthSession` 模型；6 个 owner 模型加 `user_id` 列、
  `device_id` 改 `nullable=True`，`__table_args__` 加 CHECK（`CheckConstraint("device_id IS NOT NULL OR user_id IS NOT NULL", name="ck_<表>_owner_domain")`）与新索引；docstring 注明
  "V2.2：user_id 为数据主体隔离键；device_id 为旧数据遗留列（新写入不再生成）"。
- `docs/Architecture/database-design.md`：§2 加 2.15/2.16；2.3/2.5/2.8/2.9/2.11/2.14 补 user_id 行与
  device_id 遗留说明、索引行更新；§0/§1/§3/§4/§7.1 按上文同步说明改写（2.2 api_keys、2.12
  idempotency_keys 的 PK 变更留 Task 2——本文档本任务内不得出现与 ORM 不一致的表态）。
- 更新受影响测试断言（如既有 0002/0003 测试若断言 device_id NOT NULL 或表集合）。

**验收**：先红后绿（新测试在迁移前失败）；`python -m pytest tests/contract/ tests/integration/test_alembic_migration.py`
绿；全量 pytest 绿（现有 500 测试不回归——v2.1 按 device_id 写入行为不变）；ruff/mypy/format 全绿；
`alembic upgrade head && alembic check` 零漂移（临时库，退出码 0）。

**提交信息**：`feat(account-auth): P3-1 数据地基——users/auth_sessions 新表 + 6 个直接归属表 user_id 迁移（降级列/CHECK 双非空/索引）`

### Task 2：api_keys/idempotency_keys 主键重建 + fail-closed downgrade + 旧库副本往返判别

**失败测试（先红）**：

1. `tests/contract/test_orm_database_guard.py::test_orm_idempotency_pk_order_matches_design`：
   database-design 2.12 主键行改 `` `PRIMARY KEY (user_id, path, idempotency_key)` `` 后，守卫对旧 ORM
   PK 序转红（本任务落地后转绿）。
2. `tests/integration/test_alembic_migration.py` 新增：
   - `test_alembic_api_keys_pk_rebuilt_to_user_id`：upgrade 后 PRAGMA table_info('api_keys') 主键为
     user_id、device_id 为普通 NULL 列；
   - `test_alembic_idempotency_pk_rebuilt_and_legacy_unique_kept`：idempotency_keys 主键
     (user_id, path, idempotency_key)；遗留唯一约束 (device_id, path, idempotency_key) 仍在
     （sqlite_autoindex origin='u' 列集匹配）；
   - `test_alembic_legacy_rows_preserved_after_upgrade`：在 `2a391e994f93` 旧库副本插入 devices +
     device 域行（api_keys/idempotency_keys/pdf_files/tasks/decks/cards/review_events/llm_call_attempts
     各 ≥1 行，SQL 直插）→ upgrade head → 断言旧行 device_id 原值保留、user_id 为 NULL、行数守恒；
   - `test_alembic_downgrade_fail_closed_with_user_data`：upgrade 后插入 users 行 + 任一 owner 表
     user_id 非空行 → downgrade 抛异常（PytestRaises）且表结构未变（表集合前后一致）；
   - `test_alembic_empty_and_legacy_only_downgrade_ok`：空库 upgrade→downgrade→upgrade 往返；纯旧
     device 域数据副本 upgrade→downgrade 成功（旧行保留）。
   - `test_review_events_user_client_unique_added_legacy_kept`：upgrade 后 review_events 同时存在
     唯一约束 `(user_id, client_event_id)`（另加）与 `(device_id, client_event_id)`（保留，
     sqlite_autoindex origin='u' 列集匹配两者）。

**实现**：

- 延续 Task 1 的 revision（若 Task 1 未提交则该 revision 直接扩展；若已提交则**不得**修改已提交
  迁移——在 Task 1 revision 之上加第二个 revision，`down_revision` 指向 Task 1 revision 实测值）。
- api_keys / idempotency_keys 主键重建（batch_alter_table，SQLite 需重建表）：
  - api_keys：`user_id TEXT NULL PK`（ORM `primary_key=True, nullable=True` + FK → users）、
    `device_id` 遗留 NULL 列（FK → devices 保留）、其余列不变；
  - idempotency_keys：PK 重建 `(user_id, path, idempotency_key)`（三列，`user_id` 为
    `primary_key=True, nullable=True`）、`device_id` 遗留 NULL 列、遗留
    `UNIQUE (device_id, path, idempotency_key)` 保留（unique=True 索引，SQLite 多 NULL 不冲突）。
  - 两表加 `CHECK (device_id IS NOT NULL OR user_id IS NOT NULL)`。
- `review_events`：**另加** `UNIQUE (user_id, client_event_id)`（`uq_review_events_user_client`）；
  原 `UNIQUE (device_id, client_event_id)` **保留**（目标 schema 第 6 表清单；T1 未实现，
  本任务落地——文档措辞已按"另加+保留"修正）。
- `downgrade()`（本 revision 的）：先 fail-closed 前置检查——`users` 计数 > 0，或任一 owner 表
  `user_id IS NOT NULL` 计数 > 0 → `raise RuntimeError("user-domain data exists; downgrade refused (fail closed)")`
  （在全部 DDL/DML 前）；否则执行反向 DDL。
- database-design.md：2.2 api_keys、2.12 idempotency_keys 表节改 user_id 主键/遗留 device_id 列/
  遗留唯一约束；§2 其余部分不动；§7.1 补落地记录（含 fail-closed 语义与"清理属后续独立发布"）。
- 存储守恒核验（真实命令，不进 pytest）：在临时 storage 目录 + 旧库副本上执行 upgrade，前后对比
  PDF 行数、storage 目录文件清单（`file_id/storage_key/size_bytes` manifest 一致、无 missing/orphan
  差量）——迁移不含任何 storage 操作，只证明零影响；证据写进任务报告，不落 repo。

**验收**：先红后绿；全量 pytest 绿；`ruff check .` / `ruff format --check .` / `mypy .` 全绿；
`alembic upgrade head && alembic check` 零漂移（临时库）。

**提交信息**：`feat(account-auth): P3-2 主键重建与 fail-closed——api_keys/idempotency_keys PK→user_id + 遗留唯一约束 + 新数据 downgrade 拒绝 + 旧行保留判别`
