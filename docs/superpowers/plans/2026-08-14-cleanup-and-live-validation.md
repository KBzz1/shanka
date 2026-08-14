# 清理收尾与补做验证 + 前端整合：实施计划

> **结果（2026-08-14）**：14 任务全部完成 + Task 1 fix round（4 中间表孤儿行清理）。spec §7 验收总览 8 项全勾。T13 真机 16/18（2 失败：storedSession setContent 测试代码缺陷 / fullAuthFlow 线上 502 环境问题）→ T14 修复 setContent 冲突（frontend-app a53d088）并全量重跑 18/18。三面全量回归绿（main 565/ruff/format/mypy + platform 82 + gradle 53 + assembleDebug）。详见 .superpowers/sdd/2026-08-14-cleanup-and-live-validation/task-*-report.md。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 按 spec `docs/superpowers/specs/2026-08-14-cleanup-and-live-validation-design.md` 完成 V2.3 设备架构彻底清除、9 条毛刺修复、真实联调/live/真机三件验证、上游前端视觉整合。

**Architecture:** 四段串行：① V2.3 不可逆迁移删 devices 表 + 8 表 device_id 列 + 遗留约束，models.py/契约/全仓引用同步清零；② 毛刺 9 条分域修复（平台 4 / 后端 2 / Android 2 / 文档 1）；③ 对真实后端跑 quick/full 联调与 live 真实 LLM（¥3 硬上限）；④ frontend-app merge 上游 ef2ed95（视觉取上游、逻辑取本仓库、重复视觉全量剔除），测试全集合并，最后真机验收。

**Tech Stack:** Python 3.12（Conda env `shanka-backend`）、FastAPI + SQLAlchemy 2 + Alembic（SQLite）、pytest/ruff/mypy；test-platform 纯 stdlib unittest；Android Kotlin + Compose（Gradle）。

## Global Constraints

1. 解释器 `/home/kbzz1/miniconda3/bin/conda run -n shanka-backend ...`；后端工作目录 `main/`；平台 `test-platform/`；Android `frontend-app/Front/`。
2. 四工具全绿（main/ pytest/ruff format/ruff check/mypy）+ test-platform pytest 全绿 + gradle test/assembleDebug 全绿——每个提交内相关子集绿。
3. TDD：能先红的先红；SDD 流程（implementer + reviewer 双审）。
4. 敏感信息不进日志/报告/命令参数；live 预算 ¥3 硬上限。
5. 无破坏性 git；不 push 不 PR；不碰 `docs/llm-account-long-run-v1/` 与 `docs/account-auth-test-platform-long-run-v1/` 的历史记录（追加 V2.3 决策到 PRD/Progress 属允许范围）。
6. 一致性红线：`app/schemas/` ↔ `openapi.yaml` ↔ `structure-contract.md`；ORM ↔ `database-design.md`（红线 2 守卫测试）；鉴权/幂等/错误码只在 `app/middleware/`。
7. 契约驱动单向：docs/PRD → docs/Architecture → main/，实现不得反向驱动契约。
8. 测试命名 `test_<模块>_<行为>`；主仓库所有任务从仓库根跑 brief/review 脚本，测试命令在 `main/` 内跑。
9. 迁移验证模式：临时库 `alembic upgrade head` → `alembic check`（零漂移）→ downgrade 往返（V2.3 起 downgrade 拒绝属预期）；开发库 `main/shanka.db` 直接 `alembic upgrade head`（不备份，用户已确认）。
10. V2.3 裁决（plan 对 spec 的忠实实现补充）：迁移中先 `DELETE FROM <8 表> WHERE user_id IS NULL`（旧 device 域行物理删除——spec §0「旧设备数据连同旧架构全部物理删除」），再删约束/列/表；`review_events.device_timezone` 是复习事件负载字段（IANA 时区字符串），非设备实体引用，**保留不改名**；executor/planning/scoring 的「user_id is None → 干净失败」防御分支保留（user_id 列仍 nullable），仅清注释中 device 措辞。

---

### Task 1: V2.3 迁移 revision + models.py 清理 + 迁移测试改写

**Files:**
- Create: `main/migrations/versions/<alembic 生成文件名>.py`（`alembic revision -m "v2_3_device_architecture_removal"` 生成）
- Modify: `main/infra/db/models.py`（device 残留全删，见步骤清单）
- Modify: `main/tests/integration/test_alembic_migration.py`（语义翻转改写）
- Modify: `main/services/api_key/service.py`（save_key 的 device_id 参数删除——models 删列后必须同任务删除，否则四工具红）

**Interfaces:**
- Consumes: 当前 head `e85c78b2a345`；`models.py` 现状（Device 27-36 行、8 表 device_id 列、8 个 `ck_*_owner_domain` CHECK、3 个 device 版 UNIQUE、6 个 `ix_*_device_*` 索引、IdempotencyKey `allow_partial_pks` 410 行）。
- Produces: V2.3 revision（down_revision=`e85c78b2a345`，downgrade 第一行 `raise RuntimeError("V2.3 起设备数据已物理删除，迁移不可逆；回退请恢复升级前备份")`）；models.py 零 device 实体；`save_key(owner_user_id, key)` 新签名（无 device_id 参数）；迁移测试对 V2.3 语义全绿。

- [x] **Step 1: 写迁移测试（先红）——test_alembic_migration.py 新增/改写断言**

在 `main/tests/integration/test_alembic_migration.py` 中：
1. 新增测试 `test_v2_3_downgrade_rejected`：
```python
def test_v2_3_downgrade_rejected(alembic_env):
    """V2.3 起 downgrade 显式拒绝：设备数据已物理删除，不可逆。"""
    cfg = alembic_env
    command.upgrade(cfg, "head")
    with pytest.raises(RuntimeError, match="迁移不可逆"):
        command.downgrade(cfg, "e85c78b2a345")
```
2. 改写 `_upgrade_legacy_db_with_rows` 相关测试（现 371-392 行附近）：升级到 head 后断言改为——
```python
def test_legacy_device_rows_removed_on_v2_3(alembic_env):
    """旧 device 域行随 V2.3 物理删除：user_id IS NULL 行清零、device_id 列不存在。"""
    cfg = alembic_env
    _upgrade_legacy_db_with_rows(cfg)  # 2a391e994f93 → 直插旧行 → upgrade head
    with engine_from_config(...).connect() as conn:
        for table in _OWNER_TABLES:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE user_id IS NULL")).scalar()
            assert count == 0, f"{table} 旧 device 域行未删除"
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
            assert "device_id" not in cols
        tables = _table_names(conn)
        assert "devices" not in tables
```
3. 改写现 422-450 行往返测试：downgrade 目标改 `e85c78b2a345`（不再降过 V2.3），升级回 head；原「旧行保留」断言删除。

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_alembic_migration.py -x -q`
Expected: 新测试 FAIL（V2.3 revision 不存在 / 旧行未删）。

- [x] **Step 2: 生成并写 V2.3 迁移**

**历史 revision 纪律**：P3 的 fail-closed downgrade（a7cc699f3fd8/e85c78b2a345）原样保留不改——本任务只新增 revision、不触碰既有迁移文件（spec §1.4）。

```bash
cd main && conda run -n shanka-backend alembic revision -m "v2_3_device_architecture_removal"
```
新文件内容（`upgrade()` 全文；沿用 P3 已验证的 batch/FK 关闭机制，env.py 已处理连接层 FK）：

```python
"""V2.3 设备架构彻底清除（不可逆）

Revision ID: <生成值>
Revises: e85c78b2a345
"""
from alembic import op

revision = "<生成值>"
down_revision = "e85c78b2a345"
branch_labels = None
depends_on = None

# 子表先行（FK 已由 env.py 关闭，此处按依赖序保证语义清晰）
_DELETE_ORDER = (
    "review_events",     # → cards
    "cards",             # → decks
    "decks",
    "llm_call_attempts", # → tasks
    "tasks",             # → pdf_files
    "api_keys",
    "idempotency_keys",
    "pdf_files",
)
_CHECK_CONSTRAINTS = {
    "api_keys": "ck_api_keys_owner_domain",
    "pdf_files": "ck_pdf_files_owner_domain",
    "tasks": "ck_tasks_owner_domain",
    "decks": "ck_decks_owner_domain",
    "cards": "ck_cards_owner_domain",
    "review_events": "ck_review_events_owner_domain",
    "idempotency_keys": "ck_idempotency_keys_owner_domain",
    "llm_call_attempts": "ck_llm_call_attempts_owner_domain",
}
_DEVICE_UNIQUES = (
    ("idempotency_keys", "uq_idempotency_keys_device_path"),
    ("api_keys", "uq_api_keys_device_id"),
    ("review_events", "uq_review_events_device_client"),
)
_DEVICE_INDEXES = {
    "pdf_files": ["ix_pdf_files_device_created"],
    "tasks": ["ix_tasks_device_created", "ix_tasks_task_device"],
    "decks": ["ix_decks_device_updated"],
    "cards": ["ix_cards_device_deck"],
    "review_events": ["ix_review_events_device_reviewed"],
    "llm_call_attempts": ["ix_llm_call_attempts_device_created"],
}

def upgrade() -> None:
    # 1. 旧 device 域行物理删除（user_id IS NULL 即旧 device 域行）
    for table in _DELETE_ORDER:
        op.execute(f"DELETE FROM {table} WHERE user_id IS NULL")
    # 2. 8 表删除 CHECK 双非空约束（先于删列：CHECK 引用 device_id）
    for table, ck in _CHECK_CONSTRAINTS.items():
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(ck, type_="check")
    # 3. 删除 3 个 device 版 UNIQUE（先于删列：约束引用 device_id）
    for table, uq in _DEVICE_UNIQUES:
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(uq, type_="unique")
    # 4. 删除 6 个 device_ 索引（先于删列：索引引用 device_id，SQLite 重建失败）
    for table, indexes in _DEVICE_INDEXES.items():
        with op.batch_alter_table(table) as batch:
            for idx in indexes:
                batch.drop_index(idx)
    # 5. 8 表删除 device_id 列
    for table in _DELETE_ORDER:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("device_id")
    # 6. 删除 devices 表
    op.drop_table("devices")

def downgrade() -> None:
    raise RuntimeError(
        "V2.3 起设备数据已物理删除，迁移不可逆；回退请恢复升级前备份"
    )
```

- [x] **Step 3: models.py 清理（与迁移同提交，保 alembic check 零漂移）**

按行号清单删除（`main/infra/db/models.py`）：
- 27-36：`Device` 模型整类删除；文件顶部 devices 表相关 import 不动（无）。
- 8 表 device_id 列声明删除：api_keys 65-67、pdf_files 93-95、tasks 139-141、decks 258-260、cards 296、review_events 366-368、idempotency_keys 414、llm_call_attempts 472-474。
- 8 个 `ck_*_owner_domain` CHECK 约束声明删除：api_keys 58-60、pdf_files 84-86、tasks 128-130、decks 249-251、cards 277-279、review_events 355-357、idempotency_keys 404-407、llm_call_attempts 453-456。
- 3 个 device 版 UNIQUE 删除：idempotency_keys 401-403（`uq_idempotency_keys_device_path`）、api_keys 61（`uq_api_keys_device_id`）、review_events 358（`uq_review_events_device_client`）。
- 6 个 device_ 索引删除：pdf_files 87、tasks 131+132、decks 252、cards 281、review_events 360、llm_call_attempts 465。
- IdempotencyKey 410 `__mapper_args__ = {"allow_partial_pks": True}  # noqa: RUF012` 删除；392-395 docstring 中 NULL 主键说明段与 383-385 过渡注释清理。
- ApiKey docstring 39-53 device 过渡段、其余表 docstring device 段（PdfFile 79、Task 123、Deck 244、Card 272、ReviewEvent 350、LlmCallAttempt 448）与列内联注释（92/137/257/295/471）清理。
- **保留**：`review_events.device_timezone`（376 行）——负载字段非设备实体（裁决见 Global Constraints 10）。

- [x] **Step 4: services/api_key/service.py save_key 签名更新**

```python
# 原：def save_key(owner_user_id: str, device_id: str | None = None, ...) -> ApiKey
# 新：def save_key(owner_user_id: str, ...) -> ApiKey   # 删除 device_id 参数与直插
```
Core 直插处 `device_id=None` 参数删除；调用点（tests 与 driver）同步。

- [x] **Step 5: 跑迁移验证 + 迁移测试 + 四工具**

```bash
cd main
conda run -n shanka-backend python -m pytest tests/integration/test_alembic_migration.py -x -q
# 临时库全链（7 revisions）+ check 零漂移 + V2.3 downgrade 拒绝
conda run -n shanka-backend alembic upgrade head
conda run -n shanka-backend alembic check          # 期望: No new upgrade operations detected
conda run -n shanka-backend alembic downgrade e85c78b2a345 2>&1 | tail -2   # 期望: RuntimeError 迁移不可逆
conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .
```
Expected: 迁移测试全绿；临时库 upgrade head 7 revisions 全成功、check 零漂移、downgrade 抛 RuntimeError（不是静默损坏）；四工具全绿。

- [x] **Step 6: Commit**

```bash
git add main/migrations/versions/ main/infra/db/models.py main/services/api_key/service.py main/tests/integration/test_alembic_migration.py
git commit -m "feat(v2.3): 设备架构彻底清除——不可逆迁移（删旧行/约束/列/devices 表）+ models 清理"
```

---

### Task 2: 全仓 device_id 引用清零 + 测试改写（含毛刺 #5）

**Files:**
- Modify: `main/services/tasks/executor.py:86`、`main/services/generation/planning_executor.py:436`、`main/services/generation/scoring.py:502`（注释措辞）
- Modify: `main/app/main.py:127-134`（中间件装配注释）
- Modify: `main/tests/live/driver.py`（毛刺 #5：336/337/342/668 行 + `_save_dry_run_key` 201 行）
- Modify: 测试文件清单见步骤（15 处 device_id=None 种子 + 6 个 device 域测试文件）

**Interfaces:**
- Consumes: Task 1 的 models.py 零 device 实体、save_key 新签名、V2.3 迁移。
- Produces: `grep -rn "device_id" main/ --include="*.py" | grep -v migrations/` 仅剩历史注释中的「V2.1 历史」标注或零命中；`driver.py` 无 --device-id 参数；四工具全绿。

- [x] **Step 1: 写测试锁定 driver 毛刺 #5（先红）**

`main/tests/live/test_driver_dryrun_key.py` 现 48-50 断言 `ApiKey 直插行 device_id NULL`——改写为断言「ApiKey 表无 device_id 列」（PRAGMA table_info）。`test_driver_dryrun_key.py` 其余断言（api_key 写入成功、dry-run handler 形状）保留。
`main/tests/live/` 下若有 driver argparse 测试则加断言 `--device-id` 不存在（若无此类测试，用 `python -m tests.live.driver --help` 手工验证替代，记录于报告）。

- [x] **Step 2: driver.py 毛刺 #5 修复**

`main/tests/live/driver.py`：
- 668 行 `--device-id` argparse 参数删除；
- 336 行 `device_id = args.device_id or str(uuid.uuid4())` 删除；
- 337 行「P4-4：X-Device-ID 已退出」注释更新为「Bearer only」；
- 342 行 report 字典 `"device_id": device_id,` 删除（report 字段名同步删——若下游断言引用一并删）；
- 201 行 `_save_dry_run_key` 中 `device_id=None` 直插参数删除。

- [x] **Step 3: 运行时引用清零（注释/参数）**

- `main/services/api_key/service.py` 8-10、40、61 行注释中 device 措辞清理（V2.3 历史标注）。
- `main/services/tasks/executor.py:86`：`if task.user_id is None:  # legacy device 域任务（D-06 无访问路径）` → 注释改 `# 防御：user_id 缺失的历史行（V2.3 起旧 device 域行已删除，防御分支保留）`。分支代码不动。
- `main/services/generation/planning_executor.py:436`、`main/services/generation/scoring.py:502`：同款注释措辞更新，分支不动。
- `main/app/main.py:127-134`：中间件装配顺序注释中「双头过渡窗口」段删除（V2.3 后彻底过期）；保留中间件注册代码。
- `main/app/middleware/idempotency.py:10`、`main/app/middleware/logging.py:4-5,23`、`main/app/middleware/body_capture.py:5`、`main/infra/logging.py:5-6`、`main/services/pdf/scanner.py:115`、`main/services/generation/ledger.py:70`：注释中 device 措辞清理或改「V2.1 历史」标注。

- [x] **Step 4: 测试种子/断言清零（15 处 device_id=None 种子 + 6 文件）**

- 15 处 `device_id=None` 种子参数删除（test_tasks_api.py:193、test_tasks_planning_api.py:134、test_tasks_service.py:83、test_tasks_executor.py:93/219、test_concurrency.py:101、test_batches.py:76、test_observability.py:207、test_rewrite_concurrency.py:64、test_cards_rewrite.py:59、acceptance ac04_ac07:239、ac05:155、ac06:135、test_batches_unit.py:122、test_create_task_planning.py:102、test_planning_executor.py:131、test_scoring.py:165——按实际 grep 逐处删）。
- `test_ledger.py` `_seed` 46-66：`Device(device_id="d1")` 种子与 Task device_id 参数删除（改纯 user 域）。
- `test_planning_executor.py:936-951`、`test_scoring.py:940-952`、`test_batches.py:237-253`（legacy 任务测试）：`Device("legacy-dev")` 种子删除；Task 改 `task.user_id = None` 直插（SQLAlchemy 允许，无需 devices 行）；测试名可保留 `test_*_legacy_task_no_user_fails_clean`，断言不变（干净失败）。
- `test_pdf_scanner.py` `_ensure_device`/`_seed_pending`（42-62）：改 user 域——`_seed_pending` 去掉 device_id 参数，改传 user_id；4 个 scanner 测试（64/86/106/170）调用点更新。
- `test_api_key_user_domain.py:122-142` `test_legacy_device_row_invisible_to_orm`：V2.3 后语义翻转——raw INSERT devices+device 域 api_keys 行不可能（devices 表已删），测试改为断言「api_keys 表无 device_id 列 + 旧 device 域行已随迁移删除」（或删除该测试并以其名写新断言：ORM 只见 user 域行）。
- `test_no_device_header.py:57-69`：devices 表行数不变断言改写（devices 表不存在——断言「无 devices 表」或删该断言段，保留 X-Device-ID 被忽略的主断言）。
- `test_task_e2e_user_domain.py:209-215`、`test_background_user_continuity.py:187-198`：`device_id NULL` 断言删除（列不存在，改为只断言 user_id 非空）。
- conftest.py:77 及各测试文件「X-Device-ID 已退出，仅 Bearer」helper docstring：保留（描述准确，非残留）。

- [x] **Step 5: 全仓扫描 + 四工具**

```bash
cd /home/kbzz1/shanka_backend && grep -rn "device_id" main/ --include="*.py" | grep -v "migrations/"
# 期望：零命中（或仅含「V2.1 历史」标注的注释）
cd main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .
```

- [x] **Step 6: Commit**

```bash
git add -A main/
git commit -m "feat(v2.3): 全仓 device_id 引用清零（driver 毛刺#5/测试改 user 域/注释清理）"
```

---

### Task 3: 契约同步（PRD V2.3 + database-design + structure-contract + Progress）

**Files:**
- Create: `docs/PRD/V2.3/prd_v2_3.md`
- Modify: `docs/Architecture/database-design.md`、`docs/Architecture/structure-contract.md`、`docs/Progress.md`
- Test: `main/tests/integration/test_contract_guards.py`（或现有守卫测试文件名，按仓库实际）

**Interfaces:**
- Consumes: Task 1/2 后的实际表结构（8 表无 device_id、无 devices 表、CHECK 单列 user_id）。
- Produces: PRD V2.3 权威文件；database-design 与 models.py 一致（红线 2 守卫绿）；structure-contract §9 指向 V2.3。

- [x] **Step 1: 新建 docs/PRD/V2.3/prd_v2_3.md**

内容骨架（继承 V2.2，增量 V2.3）：
```markdown
# 闪卡 App PRD V2.3
- 继承：V2.2（docs/PRD/V2.2/prd_v2_2.md）
- 变更日期：2026-08-14
- 变更性质：破坏性（结构删除）
## 变更清单
1. D-06 撤销（决策翻转）：旧设备数据连同旧架构全部物理删除（devices 表、8 表
   device_id 列、device 版约束/索引、代码与契约残留）；删除不可逆，downgrade 显式拒绝。
2. 结构变更：devices 表删除；pdf_files/tasks/decks/cards/review_events/
   llm_call_attempts/api_keys/idempotency_keys 删除 device_id 列与
   CHECK(device_id IS NOT NULL OR user_id IS NOT NULL)；owner 恒为 user_id。
3. 排除项：不回滚；device_timezone 字段保留（负载字段非设备实体）。
## 验收标准
- 空库 7 revisions 升级全链；alembic check 零漂移；V2.3 downgrade 拒绝。
- 全仓 device_id 运行时引用为零（迁移文件与 V2.1 历史标注除外）。
```

- [x] **Step 2: database-design.md 同步**

- 删除 2.1 devices 表节；8 表结构表中 device_id 行、CHECK 双非空行、device 版 UNIQUE 行、device_ 索引行删除（user_id 列改为唯一 owner 列表述）。
- §0/§1 ER 图中设备实体与关系删除。
- §7.1 更新记录追加 V2.3 行：`| V2.3 | 2026-08-14 | 设备架构彻底清除（devices 表/device_id 列/遗留约束删除，不可逆） |`。
- 全文 grep「设备/device」残留句清理（V2.1 历史决策描述句标注「V2.1 历史」后保留）。

- [x] **Step 3: structure-contract.md 同步**

- §9 对照表（资源模型↔表）指向 V2.3；全文 grep 设备残留句清理或标「V2.1 历史」。

- [x] **Step 4: Progress.md 追加**

ACC-P3 条目追加一行（历史不改写）：`- 2026-08-14 V2.3 决策翻转：D-06「不迁不删」撤销，设备数据与架构物理删除（不可逆）。`

- [x] **Step 5: 守卫全绿 + 提交**

```bash
cd main && conda run -n shanka-backend python -m pytest tests/integration -q -k "guard or contract" -q
# 期望：红线 2 守卫（ORM↔database-design）与契约守卫全绿
cd /home/kbzz1/shanka_backend && git add docs/PRD/V2.3/ docs/Architecture/ docs/Progress.md
git commit -m "docs(v2.3): 契约同步——PRD V2.3 新建 + database-design/structure-contract 设备残留清除 + Progress 决策注记"
```

---

### Task 4: 平台毛刺 1+2+3（api_smoke 守卫 / live_flow WARN / .env 相对路径）

**Files:**
- Modify: `test-platform/scenarios/baseline/api_smoke.py`（52/116/125/134 行）
- Modify: `test-platform/scenarios/flow/live_flow.py`（37 行 + 250-264 行）
- Test: `test-platform/tests/test_scenarios_api_smoke.py`、`test-platform/tests/test_scenarios_live_flow.py`

**Interfaces:**
- Consumes: 平台现状（api_smoke `check()` 软断言累积、`_same_key_post` raw urllib、live_flow `_load_env_key`）。
- Produces: api_smoke 对非 JSON 响应干净 FAIL（无 traceback）；live_flow obs bootstrap 失败有 WARN；.env 路径 `__file__` 相对推导。

- [x] **Step 1: 写失败测试（先红）**

`tests/test_scenarios_api_smoke.py` 新增：
```python
def test_api_smoke_handles_non_json_response_cleanly(self):
    """网关 502/HTML 响应时步骤干净 FAIL 而非 AttributeError/JSONDecodeError。"""
    # StubClient 构造参照本文件既有测试的 stub 用法；关键点：让 stub 对
    # POST /decks 返回 json=None 的 Response（模拟网关 502/HTML 非 JSON 响应体）
    stub = <按既有测试的 StubClient.script() 形状，Response.json=None>
    with redirect_stdout(io.StringIO()) as out:
        exit_code = api_smoke.run(stub, environment="local", username="u", password="p",
                                  same_key_post=lambda *a, **k: stub_response(json_body=None),
                                  burst=lambda *a, **k: [])
    self.assertNotEqual(exit_code, 0)  # 干净 FAIL
    self.assertNotIn("Traceback", out.getvalue())
```
（`stub_response(json_body=None)` 为局部辅助：返回 stub.Response 形状且 json=None——具体 Response 构造抄本文件既有测试的写法，先读 `tests/test_scenarios_api_smoke.py` 现有用例再写。）

`tests/test_scenarios_live_flow.py` 新增：
```python
def test_live_flow_obs_bootstrap_failure_warns(self):
    """观测账号 bootstrap 失败路径输出 WARN 且主 token 切回。"""
    # stub：obs 账号 register 返回失败 → run() 输出含 "[warn]" 且 calls 尾部为切回主 token
    ...
```
`tests/test_scenarios_live_flow.py` 新增 `.env` 路径断言：
```python
def test_env_path_is_file_relative(self):
    """_ENV_FILE 由 __file__ 推导，不含硬编码绝对路径。"""
    src = Path(live_flow.__file__).read_text()
    self.assertNotIn("/home/kbzz1", src)
```

Run: `cd test-platform && python3 -m pytest tests/test_scenarios_api_smoke.py tests/test_scenarios_live_flow.py -q`
Expected: 新测试 FAIL。

- [x] **Step 2: api_smoke.py 修复**

- 52 行 `_same_key_post`：`json.loads(...)` 包 try/except（JSONDecodeError/ValueError → 返回 `{}` 或 None）；`body1`/`body2` 取用前 isinstance dict 守卫（与文件内 114 行同款）：
```python
body = None
try:
    body = json.loads(e.read().decode() or "{}")
except (json.JSONDecodeError, UnicodeDecodeError):
    body = None
```
- 116/125/134 行：`r.json.get(...)` 前加 `if isinstance(r.json, dict)` 守卫（同 114 行模式），守卫失败走 `check(...)` 软 FAIL。

- [x] **Step 3: live_flow.py 修复**

- 37 行：`_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"`（test-platform/scenarios/flow/live_flow.py → 仓库根）。
- 250-264 行 obs 分支：`if obs is None:` 补 `print("[warn] 观测临时账号建立失败，其会话可能未撤销", ...)`（对齐 isolation.py:105-114 同款口径），并确保随后 `c.set_token(session["access_token"])` 切回主 token 的行为不变。

- [x] **Step 4: 平台全量测试 + 提交**

```bash
cd test-platform && python3 -m pytest tests/ -q && python3 -m unittest discover -s tests 2>&1 | tail -3
git add test-platform/ && git commit -m "fix(test-platform): api_smoke 非 JSON 守卫 + live_flow obs WARN + .env 相对路径（毛刺 1-3）"
```

---

### Task 5: 平台跨用户幂等复用场景（毛刺 #4）

**Files:**
- Modify: `test-platform/scenarios/isolation/isolation.py`（run() 增加跨用户幂等步骤）
- Test: `test-platform/tests/test_scenarios_isolation.py`

**Interfaces:**
- Consumes: isolation.py 现有设施（`_logout`/`_cleanup_decks`/主副 token 切换 134-140）；ShankaClient 幂等键语义（每次 idempotent=True 自动新键）。
- Produces: 场景覆盖「不同用户同 Idempotency-Key 同 body 各自成功、互不重放」；平台 77+ 测试仍全绿。

- [x] **Step 1: 写失败测试（先红）**

`tests/test_scenarios_isolation.py` 新增：
```python
def test_isolation_idempotency_key_cross_user_reuse(self):
    """不同用户同 Idempotency-Key 同 body：各自成功、互不重放（DESIGN 8.2 缺口）。"""
    # stub 按路径+token 校验：user A 与 user B 各发 POST /decks（同 key 同 body）
    # 断言：两次都 201、各建一张牌组（stub 记录两个 user 的写入）
```
（StubClient 需支持「同一 route 依 Authorization 头分派」——stub.py 无此能力则在 stub 加按 header 分派的最小扩展，仿 test_client.py 本地 HTTP server 亦可。）

- [x] **Step 2: isolation.py 增加跨用户幂等步骤**

在 run() 主流程（临时账号段）中增加：
1. 主账号与临时账号各自 `POST /decks`，**手工复用同一 Idempotency-Key 与同一 body**（raw urllib 或 client 扩展一个 `with_idempotency_key(key)` 选项——最小方案：给 ShankaClient.request 加可选 `idempotency_key: str | None = None`，非 None 时不 uuid4）。
2. 断言：两个用户均 201 且 deck_id 不同；再重放同 key 同 body 得原响应不新建（重放语义保持）。
3. 清理：两账号的牌组各自前缀清理。

- [x] **Step 3: 平台全量 + 提交**

```bash
cd test-platform && python3 -m pytest tests/ -q
git add test-platform/ && git commit -m "test(test-platform): 跨用户幂等键复用场景（毛刺 #4，DESIGN 8.2 缺口）"
```

---

### Task 6: rate_limit.py write 桶 clock 注入（毛刺 #6）

**Files:**
- Modify: `main/app/middleware/rate_limit.py`（`RateLimitMiddleware.__init__` 加 clock 透传）
- Test: `main/tests/integration/test_rate_limit.py`（或仓库中现有限流测试文件，按实际）

**Interfaces:**
- Consumes: `RateLimiter` 已支持 `clock`（53 行）；参照 `main/app/middleware/ip_limit.py:36-48` 的 `clock: Callable[[], float] | ClockLike | None = None` 透传模式。
- Produces: `RateLimitMiddleware(app, settings, *, clock=...)` 可注入固定时钟；write 桶测试不跨真实秒边界。

- [x] **Step 1: 写失败测试（先红）**

现有限流测试文件新增：
```python
def test_write_bucket_window_advance_with_manual_clock(self):
    """write 桶 60s 窗口：固定时钟推进跨窗口后复位（不依赖真实时间）。"""
    clock = _ManualClock(0.0)
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, settings=settings, clock=clock)
    # 同窗口写满 rate_limit_write_per_minute 次 → 下一次 429
    # clock.advance(61.0) → 新窗口第一次写成功（200）
```
（`_ManualClock` 若 ip_limit 测试已有则复用其形状。）

Run: `cd main && conda run -n shanka-backend python -m pytest tests/integration/test_rate_limit.py -q`
Expected: FAIL（`RateLimitMiddleware.__init__` 不接受 clock）。

- [x] **Step 2: 实现 clock 透传**

`rate_limit.py` `RateLimitMiddleware.__init__`（87-103）加 keyword-only 参数并透传 5 个桶（照 ip_limit.py:44-48）：
```python
def __init__(self, app, settings, *, clock: Callable[[], float] | ClockLike | None = None):
    ...
    self._write = RateLimiter(window=60, limit=settings.rate_limit_write_per_minute,
                              clock=clock if clock is not None else time.monotonic)
    # api_key/samples/pdf/auth 四桶同款透传
```
`main.py` 装配不传（生产默认 time.monotonic，与 ip_limit 一致）。

- [x] **Step 3: 四工具 + 提交**

```bash
cd main && conda run -n shanka-backend python -m pytest tests/integration/test_rate_limit.py -q && conda run -n shanka-backend python -m ruff check app/middleware/rate_limit.py && conda run -n shanka-backend python -m mypy app/middleware/rate_limit.py
git add main/app/middleware/rate_limit.py main/tests/ && git commit -m "fix(rate-limit): write 桶 60s 窗口 clock 注入（毛刺 #6，测试固定时钟消 flakiness）"
```

---

### Task 7: Android 毛刺 7+8（注入缝 + logout 先本地）

**Files:**
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AppViewModel.kt`（注入缝）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthViewModel.kt`（logout 顺序）
- Test: `frontend-app/Front/app/src/androidTest/java/com/qiuzhao/flashcards/FlashcardsAppTest.kt`、`frontend-app/Front/app/src/test/java/com/qiuzhao/flashcards/ui/auth/AuthViewModelTest.kt`

**Interfaces:**
- Consumes: 本仓库 AppViewModel 构造 `AppViewModel(application)` 硬编码 `KeystoreSessionStore` + `RemoteFlashcardRepository`（82-87 行）；AuthViewModel.logout()（88 行）现先网络后本地。
- Produces: `AppViewModel(application, sessionStore: SessionStore = ..., repository: RemoteFlashcardRepository = ...)` 可注入（默认生产路径不变）；`AuthViewModel.logout()` 先本地清会话（立即回登录页）再 fire-and-forget 撤销服务器 token；断网退出不阻塞。

- [x] **Step 1: 写失败测试（先红）**

`AuthViewModelTest.kt` 新增：
```kotlin
@Test
fun `logout clears local session before network revocation`() = runTest {
    // fake repository：logout 挂起不返回（模拟断网）
    // fake sessionStore：记录 clear() 调用
    val vm = AuthViewModel(fakeRepo, fakeStore, backgroundScope)
    vm.logout()
    // 断言：state 已立即变为 LoggedOut（不等网络返回）；fakeStore.clear 已调用
}
```
`FlashcardsAppTest.kt` 的 `storedSessionEntersTheMainScreen` 改写为注入缝版（不依赖后端未启动/启动）：
```kotlin
@Test
fun storedSessionEntersTheMainScreen() {
    // AppViewModel(application, sessionStore = InMemorySessionStore(...), repository = fakeRepo)
    // 断言：启动后直接进入主界面（backend 401/网络失败走 mock 路径，不依赖本机后端状态）
}
```

Run: `cd frontend-app/Front && ./gradlew test`
Expected: FAIL（logout 先本地未实现 / 注入缝不存在）。

- [x] **Step 2: 实现注入缝 + logout 先本地**

- `AppViewModel`：主构造保持 `AppViewModel(application: Application)`（AndroidViewModel 要求），内部改走次级构造：
```kotlin
class AppViewModel(
    application: Application,
    private val sessionStore: SessionStore = KeystoreSessionStore(application),
    repository: RemoteFlashcardRepository = RemoteFlashcardRepository(application, sessionStore),
) : AndroidViewModel(application) { ... }
```
（AndroidViewModel 默认构造约束允许带默认参数的额外参数；若有冲突改工厂注入，以最小方案为准。）
- `AuthViewModel.logout()`：
```kotlin
fun logout() {
    scope.launch { sessionStore.clear(); _state.value = AuthState.LoggedOut() }  // 先本地
    scope.launch { runCatching { repository.logout() } }                          // fire-and-forget 撤销
}
```

- [x] **Step 3: 40/40 + assembleDebug + 提交**

```bash
cd frontend-app/Front && ./gradlew test && ./gradlew assembleDebug
git -C /home/kbzz1/shanka_backend/frontend-app commit -am "fix(android): AppViewModel 注入缝 + logout 先本地登出再撤销（毛刺 7-8）"
```

---

### Task 8: 联调 quick/full（开发库迁移 + 真实后端）

**Files:** 无代码改动（运行验证任务）。证据写入 task 报告。

- [x] **Step 1: 开发库迁移 V2.3**

```bash
cd main && conda run -n shanka-backend alembic upgrade head
conda run -n shanka-backend alembic check
# 期望：7 revisions 全链成功；check 零漂移；旧 device 域行已删（sqlite3 抽查）
sqlite3 shanka.db "SELECT COUNT(*) FROM tasks WHERE user_id IS NULL;"   # 期望 0
sqlite3 shanka.db "SELECT name FROM sqlite_master WHERE name='devices';"  # 期望空
```

- [x] **Step 2: 启动后端**

```bash
cd main && conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port 8000
# 后台运行；curl http://127.0.0.1:8000/healthz 期望 200
```

- [x] **Step 3: 准备测试账号凭据**

凭据只从 `SHANKA_TEST_USERNAME` / `SHANKA_TEST_PASSWORD` 环境变量提供（值不得出现在命令参数/日志/报告；账号可在 local 环境注册或复用既有测试账号）。

- [x] **Step 4: runner quick + full**

```bash
cd test-platform && python3 runner/run.sh --environment local --suite quick
python3 runner/run.sh --environment local --suite full
# 期望：两套 FAIL=0；真实 HTTP 往返证据（每场景输出）记录进 task 报告
```

- [x] **Step 5: 报告 + 关停后端**

报告记录：场景清单、FAIL 计数、关键请求/响应摘录（脱敏）。停掉 uvicorn 进程。本任务无代码提交；证据计入 SDD task 报告。

---

### Task 9: live 真实 LLM（成本上限 ¥3）

**Files:** 无代码改动（运行验证任务）。

- [x] **Step 1: 前置检查**

- 仓库根 `.env`（600 权限、git 忽略）含真实 `DEEPSEEK_API_KEY`（用户已授权；Key 值绝不打印/进日志）。
- 后端运行中（Task 8 环境或重启）。
- `cd test-platform && python3 runner/run.sh --environment local --suite live`（先不加 --confirm-cost）——期望：成本闸门拒绝并打印最坏预算推导（BUDGET_FIXTURE 53 次 ≈¥1.86 ≤ ¥3 上限）与 `--confirm-cost` 提示。

- [x] **Step 2: 带确认运行 live**

```bash
cd test-platform && python3 runner/run.sh --environment local --suite live --confirm-cost
```
- 闸门放行后真实 LLM 全链（PLANNING→GENERATING→SCORING 观察路径）；任何一步接近 ¥3 即停（闸门即停逻辑）。
- 运行后批次对账（`GET /tasks/{id}/batches` 投影对账）记录：实际 attempts / token / 成本 ≤ ¥3。

- [x] **Step 3: 证据与声明**

对账数字、实际成本、未触发/未运行部分如实写入 task 报告；敏感项（Key、token、Prompt）不落盘。无代码提交。

---

### Task 10: merge 上游 ef2ed95（冲突解决落位）

**Files（frontend-app 内，merge 产生的冲突面——探索实测恰 4 个）:**
- `Front/app/src/main/java/com/qiuzhao/flashcards/ui/AppViewModel.kt`（本仓库 +45 vs 上游 +307——核心合并）
- `Front/app/src/main/java/com/qiuzhao/flashcards/ui/Screens.kt`（本仓库 +142 vs 上游整文件删除拆分 18 文件——结构性冲突）
- `Front/app/src/main/java/com/qiuzhao/flashcards/data/ImportParser.kt`（本仓库 +2/-2 vs 上游 +5/-5——双语义合并）
- `CLAUDE.md`（本仓库 +1 vs 上游 +51 全新）

**Interfaces:**
- Consumes: merge_base=`544891b1`；本仓库 P6 4 commits（60d62a3..f15457b）；上游 ef2ed95（127 files）。
- Produces: 无冲突的 merge commit；工作区编译可达（测试修复在 Task 12，本任务只保证 merge 结构与冲突裁决落地）。

- [x] **Step 1: merge 并解决冲突**

```bash
cd /home/kbzz1/shanka_backend/frontend-app
git fetch origin
git merge ef2ed95 --no-edit   # 若 fetch 后远程 head 有更新，以 merge origin/main 为准
git status   # 列出冲突文件，期望恰为 4 个（AppViewModel.kt / Screens.kt / ImportParser.kt / CLAUDE.md）
```
冲突解决规则（spec §4.2 落位）：
- **Screens.kt（结构性冲突：modify vs 删除+拆分）**：接受上游删除（`git rm`）。先 `git diff 544891b f15457b -- Screens.kt` 逐块核对本仓库 +142 改动内容：视觉/主题类（DeckTheme/DeckThemes 等）→ 上游已在 `ui/DeckTheme.kt` 落位，弃本仓库版；登录入口/逻辑类 → 记录清单，随 Task 11 在 MainActivity/Chrome.kt 层重新落地。
- **CLAUDE.md**：双方内容合并——上游 51 行（视觉/字体系统/上游项目说明）全留 + 本仓库后端对接说明段保留；若冲突块无法自动合并则手工拼接两段。
- **AppViewModel.kt**：以上游文件为基底（业务函数/pdf 流程/DataStore 全部保留），**植入本仓库 P6 的 auth 状态机集成块**（对应本仓库 82-88 行）：`sessionStore`、`repository`、`auth = AuthViewModel(...)`、`authState`。上游 `login(email, password, onResult)` 与 `register(nickname, email, password, confirmation, onResult)` 函数体（DataStore 假登录）删除，替换为委托本仓库 auth 状态机（见 Task 11）；其余函数不动。
- **ImportParser.kt**：合并双方语义——上游的 `sawQA` 段落回退抑制（上游 16/33/41 行版本）+ 本仓库的 `errors.isEmpty()` fallback 守卫（本仓库 41 行版本）。合并后逻辑：`q != null -> { sawQA = true; commit(); pendingQuestion = q }` 且 fallback 条件 `cards.isEmpty() && errors.isEmpty() && !sawQA && text.isNotBlank()`。若冲突块粒度不允许自动合并，取双方行手工拼接。
- **build.gradle.kts**：保留本仓库 `testImplementation("org.json:json:20240303")`（上游无此依赖差异，若冲突取保留）。
- 其余文件：上游新增（字体/主题/Screen 拆分/motion/navigation）全部接受；androidTest 两文件无冲突（上游未改），保留本仓库版本。

- [x] **Step 2: 验证 merge 完整性**

```bash
git status --short   # 无未解决冲突标记（UU）
git log --oneline -1  # merge commit 生成
# 本仓库 P6 资产在场性核对：
git show HEAD:Front/app/src/main/java/com/qiuzhao/flashcards/data/session/SessionStore.kt | head -3   # Keystore 版存在
git show HEAD:Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthViewModel.kt | head -3        # AuthViewModel 存在
# 上游资产在场性核对：
ls Front/app/src/main/java/com/qiuzhao/flashcards/ui/ | grep -c "Screen.kt"  # 上游拆分文件在
```
（编译验证在 Task 11/12 完成，本任务 commit 前至少 `git diff --check` 干净。）

- [x] **Step 3: Commit**

```bash
git -C /home/kbzz1/shanka_backend/frontend-app commit -m "merge: 整合上游 ef2ed95（视觉取上游/逻辑取本仓库/ImportParser 双语义合并）"
```

---

### Task 11: 接线 + 视觉单源清理

**Files:**
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AppViewModel.kt`（login/register 委托 auth）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/MainActivity.kt`（三分支外壳 + 上游视觉组件）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/AuthScreen.kt`（上游视觉，错误展示接 authState）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/Chrome.kt`（删除 accountBootstrap 假账号门控；Login/Register 路由保留）
- Modify: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/SettingsScreen.kt`（账号区接登出按钮——上游未做的例外 UI）
- Delete: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthScreens.kt`（本仓库登录视觉全删）
- Create: `frontend-app/Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthLoading.kt`（加载态——上游未做、本仓库功能必需，例外保留并按上游设计系统适配）

**Interfaces:**
- Consumes: 本仓库 AuthViewModel（state/submitting/login/register/logout/clearError）、AuthRepository；上游 LoginScreen/RegisterScreen(viewModel: AppViewModel, nav: ScreenNavigator, ...)、上游设计系统（DeckTheme/AppTheme 组件）。
- Produces: 上游视觉的登录/注册/退出触发点 → 本仓库 auth 状态机；错误文案四类映射本仓库口径；本仓库重复视觉文件清零（AuthScreens.kt 删除）。

- [x] **Step 1: AppViewModel 委托接线**

上游 `login/register` 函数体替换（上游视觉触发点不改签名，改实现）：
```kotlin
fun login(email: String, password: String, onResult: (String?) -> Unit) = viewModelScope.launch {
    // 上游 UI 字段名为 email；值作为后端 username 使用（字段语义接线，UI 视觉不变）
    val err = auth.submitLogin(email.trim(), password)
    if (err != null) onResult(err) else onResult(null)
}
fun register(nickname: String, email: String, password: String, confirmation: String, onResult: (String?) -> Unit) = viewModelScope.launch {
    val err = auth.submitRegister(email.trim(), password)   // nickname 上游本地展示用，后端无此字段
    if (err != null) onResult(err) else onResult(null)
}
```
（`submitLogin`/`submitRegister` 为 AuthViewModel 新增的挂起包装：复用现有 `login/register` 内部 submit 逻辑并返回 `String?` 错误文案——错误文案由本仓库 `authErrorMessage()` 四类映射产出：INVALID_CREDENTIALS/USERNAME_TAKEN/RATE_LIMITED/网络错误；implementer 以最小改动实现，不破坏现有 40 测试中 login/register 语义。）

- [x] **Step 2: MainActivity 三分支外壳 + 上游视觉组件 + 门控替换**

```kotlin
setContent {
    val appViewModel: AppViewModel = viewModel()
    val authState by appViewModel.authState.collectAsState()
    when (val s = authState) {
        is AuthState.CheckingSession -> AuthLoadingScreen(appViewModel)   // 例外保留的加载态
        is AuthState.LoggedOut -> LoginScreen(appViewModel, rememberNavigator(), firstLaunch = true)  // 上游视觉
        is AuthState.LoggedIn -> FlashcardsApp(appViewModel)              // 上游主界面
    }
}
```
- **门控替换（关键）**：上游 `Chrome.kt` L159 的 `accountBootstrap.loaded && account == null → navigate(FirstLogin)` 假账号门控（DataStore 三键 ACCOUNT_LOGGED_IN/EMAIL/NICKNAME）**删除**——真实门控统一由 MainActivity 的 authState 三分支承担，避免双门控/双登录屏。上游 FirstLogin/Login/Register 三路由保留（App 内 Settings 跳登录等导航用），但进入条件不再由 accountBootstrap 驱动。
- 上游 `LoginScreen/RegisterScreen` 内部「切到注册/登录」的导航沿用上游 ScreenNavigator（AppRoute.Login/Register 切换）；本仓库三分支只守「登录态与否」两个门。
- **登出入口接线（上游未做、本仓库功能必需的例外 UI）**：上游 AuthScreen 全文件无登出触发点；本仓库 logout 已实现但 Settings 未接。在**上游 SettingsScreen** 的账号区（onAvatar 附近）接登出按钮：点击 → `appViewModel.auth.logout()`（先本地登出立即回登录屏 + 后台撤销，Task 7 语义）→ 上游视觉组件样式呈现按钮。
- 上游 accountBootstrap 相关 DataStore 键（ACCOUNT_LOGGED_IN/ACCOUNT_EMAIL/ACCOUNT_NICKNAME）与 `accountBootstrap` StateFlow 弃用清理：昵称展示改用 `auth.state` 中 `LoggedIn(user.username)`；`LocalAccount` 类型与 AccountBootstrap 数据类随假账号壳删除（视觉组件若引用 nickname 参数则改传 username）。

- [x] **Step 3: 视觉单源清理（全量剔除重复视觉）**

- 删除 `ui/auth/AuthScreens.kt` 全文件（AuthLoadingScreen 除外——提取到新文件 `ui/auth/AuthLoading.kt`，用上游 DeckTheme 的 Surface/文本组件重写视觉，加载文案「正在检查登录状态…」）。
- `ui/auth/` 目录最终只剩 `AuthViewModel.kt` + `AuthLoading.kt`（逻辑 + 例外 UI）；上游视觉只有 `ui/AuthScreen.kt` 一份（视觉单源）。
- 清理后 grep：`frontend-app` 内不存在本仓库登录视觉组件引用（LoginScreen/RegisterScreen 只来自 `com.qiuzhao.flashcards.ui.AuthScreenKt`）。

- [x] **Step 4: 错误文案映射核对**

上游 AuthScreen 的 `onResult(String?)` 展示路径（AuthMessage 组件）已存在；接线后四类错误自然呈现本仓库文案。核对上游 AuthScreen 中是否有「请输入邮箱」「密码至少需要 6 位」等本地校验文案——保留上游本地校验（视觉/交互取上游），仅在真正提交后端时走本仓库文案。冲突处按「后端错误文案 = 本仓库口径」裁决。

- [x] **Step 5: 编译 + 提交**

```bash
cd frontend-app/Front && ./gradlew assembleDebug   # 期望 BUILD SUCCESSFUL（测试修复在 Task 12）
git -C /home/kbzz1/shanka_backend/frontend-app commit -am "feat(integrate): 上游 AuthScreen 接本仓库 auth 状态机 + 视觉单源（删本仓库重复视觉）"
```

---

### Task 12: 测试全集合并修复 + 登录全链接线 instrumented 场景

**Files:**
- Modify: `frontend-app/Front/app/src/androidTest/java/com/qiuzhao/flashcards/FlashcardsAppTest.kt`（适配上游视觉组件）
- Modify: `frontend-app/Front/app/src/androidTest/java/com/qiuzhao/flashcards/data/remote/BackendClientInstrumentedTest.kt`（登录全链场景扩展）
- Modify: `frontend-app/Front/app/src/test/java/com/qiuzhao/flashcards/data/ImportParserTest.kt`（双方语义合并后的断言）
- Test（上游新增，跑通即可，视签名变化微调）: `TypographySystemTest.kt`、`AppNavigatorTest.kt`、`ReviewSchedulerTest.kt`

**Interfaces:**
- Consumes: Task 10/11 的整合产物（上游视觉 + auth 状态机接线）。
- Produces: 双方测试全集全绿（本仓库逻辑测试 + 上游新增测试 + androidTest 适配版）；新增登录全链 instrumented 场景（注册→登录→登出→重登 + 四类错误文案）。

- [x] **Step 1: JVM 测试全集跑通（先看红）**

```bash
cd frontend-app/Front && ./gradlew test
```
修复方向：
- `ImportParserTest`：断言覆盖双语义（sawQA 抑制 + errors 守卫），按 Task 10 合并后行为补/改断言。
- `AuthViewModelTest/AuthClientContractTest/SessionStoreContractTest`：不依赖 UI，预计全绿（logout 先本地改动在 Task 7 已锁定）。
- 上游 `TypographySystemTest/AppNavigatorTest/ReviewSchedulerTest`：编译/运行错误按上游签名修复（若引用被删的 Screens.kt 符号则改指 Chrome.kt/新 Screen 文件）。

- [x] **Step 2: androidTest 适配上游视觉**

`FlashcardsAppTest`：
- `loggedOutStartupLandsOnTheLoginScreen`：文本断言改上游 AuthScreen 的可见节点（登录按钮/标题文案按上游实现，运行后以 `onNodeWithText` 实际文案锁定）。
- `storedSessionEntersTheMainScreen`：沿用 Task 7 注入缝，断言进入上游主界面（FlascardsApp 根节点）。

- [x] **Step 3: 新增登录全链 instrumented 场景**

`BackendClientInstrumentedTest.kt` 扩展（或新增 `AuthFlowInstrumentedTest.kt`）：
```kotlin
@Test
fun fullAuthFlowRegisterLoginLogoutRelogin() {
    // 真实后端（adb reverse 或 10.0.2.2）：
    // 1. register 新账号（随机后缀，避免重名）→ 期望成功进主界面
    // 2. logout → 回登录屏
    // 3. login 同账号 → 再进主界面（重登链路）
    // 4. 错误路径：错密码 → 文案 INVALID_CREDENTIALS 口径；重复注册 → USERNAME_TAKEN；
    //    限流/网络错误路径（若真实触发成本高，用 fake transport 锁定文案映射）
}
```
四类错误文案断言：`INVALID_CREDENTIALS`/`USERNAME_TAKEN`/`RATE_LIMITED`/网络错误 → 本仓库 `authErrorMessage()` 对应中文文案（与 AuthViewModelTest 已有断言一致）。

- [x] **Step 4: 全集全绿 + assembleDebug + 提交**

```bash
cd frontend-app/Front && ./gradlew test && ./gradlew assembleDebug && ./gradlew assembleDebugAndroidTest
git -C /home/kbzz1/shanka_backend/frontend-app commit -am "test(integrate): 双方测试全集合并 + 登录全链 instrumented 场景 + androidTest 适配上游视觉"
```

---

### Task 13: 真机验收（整合后）

**Files:** 无代码改动（运行验证任务）。

- [x] **Step 1: 环境前置**

```bash
adb devices   # 期望 adc60f1a device
adb reverse tcp:8000 tcp:8000   # 真机 → 本机后端
cd main && conda run -n shanka-backend uvicorn app.main:app --host 127.0.0.1 --port 8000  # 后端在
```

- [x] **Step 2: connectedDebugAndroidTest**

```bash
cd frontend-app/Front && ./gradlew connectedDebugAndroidTest
# 期望：BackendClientInstrumentedTest + FlashcardsAppTest（含登录全链）真机全绿
```

- [x] **Step 3: 人工目检**

在真机上打开 App 肉眼核对：登录界面视觉（字体/主题/动画背景）与上游一致（对照上游 ef2ed95 的界面）；注册/登录/登出操作顺畅；错误提示文案为本仓库口径。目检结论写入 task 报告（不设自动化视觉测试，spec §4.3）。

- [x] **Step 4: 视觉单源文件清单证据**

```bash
cd /home/kbzz1/shanka_backend/frontend-app
git ls-files Front/app/src/main/java/com/qiuzhao/flashcards/ui/   # 视觉文件清单
git log --oneline --all -- Front/app/src/main/java/com/qiuzhao/flashcards/ui/auth/AuthScreens.kt  # 确认已删除
```
清单对比结论写入 task 报告（本仓库重复视觉零残留、上游视觉文件单源）。

---

### Task 14: 全量回归 + 收尾（毛刺 #9 + 验收勾选）

**Files:**
- Modify: `.superpowers/sdd/2026-08-14-test-platform-v2/task-3-report.md`（口径修正——毛刺 #9 前半）
- Modify: `docs/superpowers/specs/2026-08-14-cleanup-and-live-validation-design.md`（§7 验收总览勾选）

- [x] **Step 1: 毛刺 #9 文档口径修正**

- `.superpowers/sdd/2026-08-14-test-platform-v2/task-3-report.md`：§6 文件计数口径（11 vs 14）统一为 git --stat 惯例口径；复跑次数口径（8 次 vs 6/6）统一为「同一证据单一口径」——两处在报告末尾追加勘误注（不改写历史正文，只追加「2026-08-14 勘误」行，SDD 记录纪律）。
- `main/app/main.py` 双头注释已由 Task 2 清理（此处核对即可）。

- [x] **Step 2: 三面全量回归**

```bash
cd main && conda run -n shanka-backend python -m pytest -q && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m ruff format --check . && conda run -n shanka-backend python -m mypy .
cd ../test-platform && python3 -m pytest tests/ -q
cd ../frontend-app/Front && ./gradlew test && ./gradlew assembleDebug
```

- [x] **Step 3: 验收总览勾选 + 提交**

spec §7 勾选全部条目（以各 task 报告证据为准）；本计划文件的完成记录。提交：
```bash
git add docs/superpowers/ && git commit -m "docs: 清理收尾与验证工作包完成——验收总览勾选 + 口径勘误"
```

---

## 附录 A：与 spec 的映射

| spec 节 | 任务 |
| --- | --- |
| §1.1 迁移 | Task 1 |
| §1.2 契约同步 | Task 3 |
| §1.3 代码清理 | Task 1+2 |
| §1.4 连带项（fail-closed 保留/迁移测试改写） | Task 1 |
| §2 毛刺 1-3 | Task 4 |
| §2 毛刺 4 | Task 5 |
| §2 毛刺 5 | Task 2 |
| §2 毛刺 6 | Task 6 |
| §2 毛刺 7-8 | Task 7 |
| §2 毛刺 9 | Task 14（+ Task 2 注释部分） |
| §3.1 联调 | Task 8 |
| §3.2 live | Task 9 |
| §4.2 整合规则 | Task 10+11 |
| §4.3 整合验收 | Task 12 |
| §5 真机 | Task 13 |
| §7 验收总览 | Task 14 |
| §4.4 fork/push/PR | 计划外——执行前需用户再次确认 |

## 附录 B：执行顺序与依赖

```
Task 1 → Task 2 → Task 3 → Task 4/5/6（互不依赖，可任意顺序）→ Task 7 → Task 8 → Task 9
                        → Task 10 → Task 11 → Task 12 → Task 13 → Task 14
```
Task 8 依赖 Task 1（开发库迁移）；Task 10 依赖 Task 7（Android 毛刺在 merge 前基线修好）；
SDD 派发仍按序单任务执行（不同时派多个 implementer）。
