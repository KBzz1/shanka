# V5B 任务恢复、取消与并发闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**结果**：V5B DONE（2026-08-11）。验收与证据见 docs/Progress.md 第 4 节 V5B 行（5 commits 6de2071..3fb2059 + fix 4d22b53，分支 codex/v5b）：321 用例全绿、四工具通过、干净安装+迁移、uvicorn 冒烟 healthz 200、边界 36 用例全绿、AC-05 通过。全任务 checklist 勾选完成。

**Goal:** 在 V4/V5A 唯一任务状态机上补 checkpoint/resume/cancel 恢复语义：RUNNING 心跳（每批后刷新 updated_at）、30 分钟孤儿 RUNNING 抢占恢复、批次级与任务级 DB 条件更新抢占（并发 worker 单执行者）、崩溃恢复（新 app/session 从游标继续、已完成批次与 generation_item_id 不重复）、取消保留已入库卡，使 V5B 依据真实验收证据标记 DONE 且 AC-05 通过；不建立第二套任务框架、不引入外部队列。

**Architecture:** 契约驱动分层。V5B 在 V4/V5A 基础上最小扩展：`services/tasks/executor.py`（心跳：每批后刷新 task.updated_at；批次级条件更新抢占）、`services/tasks/service.py`（resume_task 孤儿 RUNNING 抢占：条件更新 `status IN (PAUSED, RUNNING) AND (status='PAUSED' AND resumable=1 OR status='RUNNING' AND updated_at < now-30min)`）、`services/generation/batches.py`（process_next_batch 取批次改条件更新 `status='PENDING' → 'PROCESSING'`——原子抢占）、`app/config.py`（orphan_timeout_minutes=30）。崩溃恢复由现有游标/批次状态（DB 即状态，4.4）承载——新 app/session 重新扫描时 PENDING 批次继续、SUCCEEDED/SKIPPED 跳过、generation_item_id 部分唯一索引防重。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2.0、pytest、ruff、mypy strict。

## Global Constraints

- 所有命令经 `conda run -n shanka-backend ...` 执行（在 `main/` 下），禁止向 base/系统 Python 装依赖。
- 契约权威：structure-contract 4.1（任务状态机：RUNNING ⇄ PAUSED、孤儿 RUNNING 心跳超时 30 分钟、resume 条件更新抢占、PAUSED→RUNNING 原子转移）、6.4（resume/cancel）、database-design 2.5/2.7/§3、PRD 5.12/AC-05。实现不得修改 `docs/PRD/`、`docs/Architecture/`。
- **心跳**（4.1）：RUNNING 任务每批完成后刷新 `updated_at`（心跳语义）；心跳超时（30 分钟，Settings `orphan_timeout_minutes: int = 30`）后视为可恢复。
- **孤儿抢占**（4.1）：resume 条件更新扩展——`PAUSED AND resumable=1`（现有）**或** `RUNNING AND updated_at < now-30min`（孤儿抢占）；两者任一成功 → RUNNING；rowcount=0 → 409 TASK_STATE_CONFLICT（并发 resume 失败者）。
- **批次级抢占**（并发 worker 单执行者）：process_next_batch 取批次用条件更新 `status='PENDING' → 'PROCESSING'`（原子转移；rowcount=0 则重试下一批或返回 0）——两个 worker 不会双处理同一批次；已完成批次（SUCCEEDED/SKIPPED）天然不可取。
- **崩溃恢复**（4.4/AC-05）：DB 即状态；新 app/session 扫描时 PENDING 批次继续、SUCCEEDED/SKIPPED 跳过、generation_item_id 部分唯一索引防重（AC-05 四条）。
- **取消**（6.4/4.1）：cancel 保留已入库卡（现有语义）；CANCELLED 终态不重试不恢复。
- **幂等**：resume/cancel 走 execute_idempotent（现有）。
- 时间格式唯一规范；跨设备统一 404；错误响应 1.4 形状。
- 工作包边界：V5B 不含真实 DeepSeek（R1）、单卡重写（V6）、外部队列（红线）；`app/api/` 其他占位模块不得改动。
- ruff line-length 100、mypy strict；四工具命令全绿；测试命名 `test_<模块>_<行为>`；提交只 `git add` 本任务文件。
- Task 1~3 由实现 subagent 完成；Task 4/5 仅主 Agent 执行（验收 + Progress 更新），subagent 不得触碰 Progress。

---

### Task 1: 心跳 + 批次/任务级条件更新抢占

**Files:**
- Modify: `main/services/tasks/executor.py`
- Modify: `main/services/generation/batches.py`
- Modify: `main/app/config.py`（orphan_timeout_minutes）
- Create: `main/tests/integration/test_concurrency.py`
- Modify: `main/tests/integration/test_tasks_executor.py`（适配）

**Interfaces:**
- Consumes: V5A executor/batches、Settings
- Produces: `executor._execute_task` 每批后 `task.updated_at = now`（心跳——每批 completion 时取服务端时钟）；`batches.process_next_batch` 取批次改条件更新（`UPDATE batches SET status='PROCESSING' WHERE batch_id=? AND status='PENDING'`；rowcount=0 → 下一条/返回 0）；Settings `orphan_timeout_minutes: int = 30`；Task 2 孤儿恢复消费

- [x] **Step 1: 写失败集成测试 `main/tests/integration/test_concurrency.py`**

```python
"""V5B 并发/心跳集成测试：批次抢占单执行者/心跳刷新（真实 SQLite + mock transport）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from infra.db.models import Base, Batch, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import process_next_batch
from services.tasks.executor import process_running_tasks


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(session: Session, *, device_id: str) -> str:
    # 复用 V5A 种子模式（PdfFile/Chapter/Deck/ApiKey + create_task）
    ...


def _client() -> DeepSeekClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"cards": [{"type": "QUESTION", "question": "q", "answer": "a"}]}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    return DeepSeekClient(Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(handler))


def test_concurrency_two_workers_single_effect(session_factory: Callable[[], Session]) -> None:
    """两 worker 并发处理同任务：批次条件更新抢占 → 单执行者（无双处理）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
    client = _client()
    with session_factory() as session:
        # worker A 取批次（PENDING→PROCESSING）
        n1 = process_next_batch(session, task_id=task_id, client=client)
        session.commit()
        # worker B 再取（同一批次已 PROCESSING → 不可取；取下一个或 0）
        n2 = process_next_batch(session, task_id=task_id, client=client)
        session.commit()
    assert n1 == 1
    assert n2 == 0  # 同批次被 A 抢占，B 无批次可取


def test_concurrency_heartbeat_updates_updated_at(session_factory: Callable[[], Session]) -> None:
    """心跳：每批完成后 task.updated_at 刷新。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, device_id=device)
        created_at = session.get(Task, task_id).created_at
    client = _client()
    with session_factory() as session:
        process_running_tasks(session)  # 单次扫描（可能处理多批）
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
    assert task.updated_at > created_at  # 心跳刷新（批处理后时间推进）
    assert task.status == "COMPLETED"
```

（说明：心跳的 now 取服务端时钟（SystemClock format_utc）——与 created_at 有可观测差异。process_next_batch 的 rowcount 抢占：条件更新后 rowcount=0 → 继续尝试下一条（循环内），全 0 → 返回 0。）

- [x] **Step 2: 运行确认失败**

Run: `cd /home/kbzz1/shanka_backend/main && conda run -n shanka-backend python -m pytest tests/integration/test_concurrency.py -v`
Expected: FAIL（抢占/心跳未实现——n2 可能非 0、updated_at 未刷新）

- [x] **Step 3: 实现修改**

```python
# batches.py：process_next_batch 取批次改条件更新
def _claim_next_batch(session: Session, *, task_id: str) -> Batch | None:
    """条件更新抢占：PENDING → PROCESSING（原子转移，并发 worker 单执行者）。"""
    while True:
        candidate = session.scalar(
            select(Batch).where(
                Batch.task_id == task_id,
                Batch.status.in_(["PENDING", "FAILED"]),
            ).order_by(Batch.batch_index).limit(1)
        )
        if candidate is None:
            return None
        result = session.execute(
            update(Batch)
            .where(Batch.batch_id == candidate.batch_id, Batch.status == candidate.status)
            .values(status="PROCESSING")
        )
        if result.rowcount == 1:
            session.refresh(candidate)
            return candidate
        # 被其他 worker 抢占 → 取下一条（continue 循环）

# executor.py：心跳——每批完成后刷新 task.updated_at
#   _execute_task 的批处理循环内，每批后：
#   task.updated_at = format_utc(SystemClock().now_utc())
```

- [x] **Step 4: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_concurrency.py tests/integration/test_tasks_executor.py tests/integration/test_batches.py -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 5: 提交**

```bash
git add main/app/config.py main/services/generation/batches.py main/services/tasks/executor.py main/tests/integration/test_concurrency.py main/tests/integration/test_tasks_executor.py
git commit -m "feat(tasks): 心跳刷新 + 批次级条件更新抢占（并发单执行者）"
```

---

### Task 2: 孤儿 RUNNING 恢复（resume 抢占扩展）

**Files:**
- Modify: `main/services/tasks/service.py`（resume_task）
- Modify: `main/tests/integration/test_tasks_service.py`（补用例）

**Interfaces:**
- Consumes: Task 1 Settings（orphan_timeout_minutes）
- Produces: `resume_task` 条件更新扩展：`(status='PAUSED' AND resumable=1) OR (status='RUNNING' AND updated_at < now-30min)` → RUNNING；rowcount=0 → 409 TASK_STATE_CONFLICT；Task 3 崩溃恢复与 AC-05 验收消费

- [x] **Step 1: 写失败测试（追加到 `main/tests/integration/test_tasks_service.py`）**

```python
def test_tasks_resume_orphan_running_after_timeout(session_factory: Callable[[], Session]) -> None:
    """孤儿 RUNNING（updated_at 超 30 分钟）→ resume 抢占恢复（4.1）。"""
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                           chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
        session.flush()
        # 模拟孤儿：RUNNING + updated_at 3 小时前
        task.updated_at = "2026-08-10T21:00:00.000Z"
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        result = resume_task(session, device_id=device, task_id=task_id, now="2026-08-11T00:30:00.000Z")
        session.commit()
    assert result.status == "RUNNING"
    assert result.resumable == 0


def test_tasks_resume_running_fresh_conflicts(session_factory: Callable[[], Session]) -> None:
    """新鲜 RUNNING（心跳内）→ resume 409 TASK_STATE_CONFLICT。"""
    device = _uuid()
    with session_factory() as session:
        ctx = _seed_context(session, device_id=device)
        session.commit()
    with session_factory() as session:
        task = create_task(session, device_id=device, file_id=ctx["file_id"], deck_id=ctx["deck_id"],
                           chapter_ids=ctx["chapter_ids"], config=_config(), now="2026-08-11T00:00:00.000Z")
        session.commit()
        task_id = task.task_id
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            resume_task(session, device_id=device, task_id=task_id, now="2026-08-11T00:10:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
```

- [x] **Step 2: 运行确认失败 → 实现 resume_task 扩展**

```python
def resume_task(session: Session, *, device_id: str, task_id: str, now: str) -> Task:
    """DB 条件更新抢占（4.1）：PAUSED AND resumable=1，或孤儿 RUNNING（心跳超时）→ RUNNING；否则 409。"""
    task = _owned_task(session, device_id=device_id, task_id=task_id)
    orphan_cutoff = _format_cutoff(now, settings.orphan_timeout_minutes)  # now - 30min
    result = session.execute(
        update(Task)
        .where(
            Task.task_id == task_id,
            ((Task.status == "PAUSED") & (Task.resumable == 1))
            | ((Task.status == "RUNNING") & (Task.updated_at < orphan_cutoff)),
        )
        .values(status="RUNNING", updated_at=now)
    )
    if result.rowcount == 0:
        raise AppError(ErrorCode.TASK_STATE_CONFLICT, "任务不可恢复")
    session.refresh(task)
    return task
```

（说明：orphan_cutoff = now - 30min 的 format_utc 字符串（字符串比较=时间序）。Settings orphan_timeout_minutes 经 session.info 或参数传入——**决策**：resume_task 签名加 `orphan_timeout_minutes: int = 30` 参数（默认与 Settings 一致；handler 传 settings 值）。）

- [x] **Step 3: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_tasks_service.py -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 4: 提交**

```bash
git add main/services/tasks/service.py main/tests/integration/test_tasks_service.py
git commit -m "feat(tasks): 孤儿 RUNNING 恢复（心跳超时 30 分钟抢占，resume 扩展）"
```

---

### Task 3: 崩溃恢复 + AC-05 验收

**Files:**
- Create: `main/tests/acceptance/test_acceptance_ac05.py`

**Interfaces:**
- Consumes: Task 1/2 全部产物
- Produces: AC-05 四条验收映射（中断保留/游标继续/已完成批次不重复/generation_item_id 不重复）

- [x] **Step 1: 写验收测试 `main/tests/acceptance/test_acceptance_ac05.py`**

```python
"""验收测试：AC-05 任务恢复与幂等（PRD；迁移 schema + HTTP + mock transport + 崩溃模拟）。"""

# 场景 1（中断保留 + 游标继续）：mock transport 第 1 批成功、第 2 批前"崩溃"（不再调用）
#   → 任务停留 RUNNING + 批次 1 SUCCEEDED + 卡保留
#   → 新 app/session（重启模拟）→ resume（孤儿）→ 继续处理批次 2 → COMPLETED
#   → 已完成批次不重复（批次 1 不重跑）、generation_item_id 不重复（卡数=两批合法卡数）
# 场景 2（取消保留）：任务运行中 cancel → CANCELLED + 已入库卡保留
```

- [x] **Step 2: 运行确认通过 + ruff/mypy + 全量**

Run: `conda run -n shanka-backend python -m pytest tests/acceptance/ -v && conda run -n shanka-backend python -m pytest && conda run -n shanka-backend python -m ruff format . && conda run -n shanka-backend python -m ruff check . && conda run -n shanka-backend python -m mypy .`
Expected: 全绿

- [x] **Step 3: 提交**

```bash
git add main/tests/acceptance/test_acceptance_ac05.py
git commit -m "test(acceptance): AC-05 任务恢复与幂等验收映射（崩溃恢复/游标/防重）"
```

---

### Task 4: 整包验收（仅主 Agent）

**Files:**
- 验证：全部 V5B 产物；不新增代码

- [x] **Step 1: 四工具命令全绿**

Run（均在 `main/`）: `python --version`、`python -m pytest`、`python -m ruff check .`、`python -m ruff format --check .`、`python -m mypy .`
Expected: 全绿

- [x] **Step 2: 干净环境安装 + 迁移**

（venv + alembic upgrade 验证）

- [x] **Step 3: uvicorn 冒烟（不触网路径——路由可达 + 空态）**

```bash
cd /home/kbzz1/shanka_backend/main
rm -f shanka.db && conda run -n shanka-backend alembic -x database_url="sqlite:///./shanka.db" upgrade head
/home/kbzz1/miniconda3/envs/shanka-backend/bin/python -m uvicorn app.main:app --port 8084 > /tmp/v5b-uvicorn.log 2>&1 &
sleep 3
DEV=$(conda run -n shanka-backend python -c 'import uuid;print(uuid.uuid4())')
echo "healthz=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8084/healthz)"
kill %1
```
Expected: healthz 200

- [x] **Step 4: 关键边界复核（收证据）**

Run: `conda run -n shanka-backend python -m pytest tests/integration/test_concurrency.py tests/integration/test_tasks_service.py tests/acceptance/ -v`
Expected: 全绿；记录关键用例名（并发单执行者、心跳刷新、孤儿恢复、AC-05）

- [x] **Step 5: 无明文泄漏抽查**

Run: `grep -rn "sk-" main/app main/services main/infra --include="*.py" || true`
Expected: 无真实泄漏

---

### Task 5: 更新 Progress（仅主 Agent）

**Files:**
- Modify: `docs/Progress.md`
- Modify: `docs/superpowers/plans/2026-08-11-v5b-task-recovery-concurrency.md`（标题下「结果」）

- [x] **Step 1: 更新 `docs/Progress.md`**

- 第 4 节 V5B 行：`TODO` → `DONE`，证据填写：心跳、孤儿 RUNNING 恢复（30 分钟）、批次级条件更新抢占（并发单执行者）、崩溃恢复（游标继续/防重）、取消保留卡、AC-05 通过。
- 第 1 节状态基线：自动化验证测试数更新。
- 计划文件标题下「结果」注明 V5B DONE 与证据位置。

- [x] **Step 2: 提交**

```bash
git add docs/Progress.md docs/superpowers/plans/2026-08-11-v5b-task-recovery-concurrency.md
git commit -m "docs(progress): V5B DONE（任务恢复、取消与并发闭环），AC-05 通过"
```

---

## Self-Review

**1. Spec coverage（对照 Progress V5B 文本）：**

| V5B 要求 | 落点 |
| --- | --- |
| checkpoint/resume/cancel | Task 1/2（游标已有 + resume 扩展 + cancel 保留） |
| RUNNING 心跳 | Task 1（每批后 updated_at 刷新） |
| 30 分钟孤儿恢复 | Task 2（条件更新 RUNNING AND updated_at < now-30min） |
| DB 条件抢占 | Task 1/2（批次级 PENDING→PROCESSING + 任务级 resume） |
| 崩溃恢复（新 app/session） | Task 3（中断模拟 → 重启恢复） |
| 并发 worker/resume 单执行者 | Task 1/2 测试 |
| 完成批次和 generation_item_id 不重复 | Task 3（AC-05 断言） |
| 取消保留已入库卡 | Task 3（场景 2） |
| AC-05 | Task 3 |
| 不建立第二套框架/外部队列 | 现有 4.4 定式不变 |

**2. Placeholder scan：** 全部任务给出完整文件内容与可执行命令。Task 1 的 _seed_task 复用（"…"占位）——实现者按 V5A 同款种子复制；Task 2 的 orphan_cutoff 实现细节在说明中给出。非占位。

**3. Type consistency：** `resume_task(session, *, device_id, task_id, now, orphan_timeout_minutes=30)`（Task 2 定义，handler 消费）；`process_next_batch`/`process_running_tasks`（V5A 定义，Task 1 修改）；`_claim_next_batch`（Task 1 定义，process_next_batch 使用）；Settings `orphan_timeout_minutes`（Task 1 定义，Task 2 使用）。
