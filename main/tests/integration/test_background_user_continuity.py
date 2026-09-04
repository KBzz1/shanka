"""LLM 后台 user_id 接续判别测试（P5-1；DESIGN §6 / WORKER_PROMPT 目标二 §4 冻结语义）。

锁定：tasks.user_id 为后台执行身份；executor 链路零 session/token 依赖；logout 或
session 过期后任务继续；operation_key/CAS/账本幂等不依赖 session；跨用户 404；匿名
/metrics 无身份聚合。全部判别测试，不改 LLM 语义。

基建复用 test_task_e2e_user_domain.py 的 mock transport 全链路模式（零种子直写）。
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine, create_session_factory
from services.pdf.scanner import scan_once as scan_pdfs
from services.tasks.executor import scan_once as scan_tasks
from tests.conftest import auth_headers
from tests.integration.test_task_e2e_user_domain import FakeClient, _client_factory

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根
SAMPLE = REPO_ROOT / "res" / "AI-Agents-in-Depth-zh-CN.pdf"

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（与 E2E 同款：后台循环隔离 + 测试加密密钥）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "continuity.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,
        task_scan_interval_seconds=3600.0,
        api_key_encryption_key="aa" * 32,
        metrics_auth_exempt=True,  # 指标内容断言用；生产默认 Bearer 收紧（R25-07 同批）
    )
    monkeypatch.setattr("app.api.api_key.DeepSeekClient", FakeClient)
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _scan_pdfs(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    scan_pdfs(app.state.session_factory, storage=app.state.storage)


def _create_task_before_executor(
    client: TestClient, user: dict[str, str], db_path: Path
) -> tuple[str, str]:
    """E2E 建任务前半程（Key/建项目/牌组/任务创建/样卡确认，V2.5 项目归属入口），返回 (task_id, deck_id)。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    resp = client.put(
        "/api-key", json={"api_key": "sk-test-cont-1234"}, headers={**user, **_idem()}
    )
    assert resp.status_code == 200
    resp = client.post("/projects", json={"name": "continuity"}, headers={**user, **_idem()})
    assert resp.status_code == 201
    project_id = resp.json()["project_id"]
    with SAMPLE.open("rb") as f:
        resp = client.post(
            f"/projects/{project_id}/materials/pdf",
            files={"file": ("book.pdf", f, "application/pdf")},
            headers={**user, **_idem()},
        )
    assert resp.status_code == 201
    _scan_pdfs(client)
    project = client.get(f"/projects/{project_id}", headers=user).json()
    chapters = project["chapters"]
    assert project["materials"][0]["status"] == "PARSED"
    deck_resp = client.post(
        "/decks", json={"name": "D", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert deck_resp.status_code == 201
    resp = client.post(
        f"/projects/{project_id}/tasks",
        json={
            "deck_id": deck_resp.json()["deck_id"],
            "chapter_ids": [c["chapter_id"] for c in chapters[:2]],
            "generation_config": {
                "coverage_mode": "COMPACT",
                "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
            },
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    # 样卡阶段（V2.5 确认式启动）：触发持久化生成（DRAFT → SAMPLE_GENERATING）→
    # 样卡 worker 扫描 → AWAITING_SAMPLE_CONFIRMATION → start 确认进入 GENERATING
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    _run_executor_until_done(db_path)
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    return task_id, deck_resp.json()["deck_id"]


def _run_executor_until_done(db_path: Path) -> None:
    """显式 executor 扫描直至无 PENDING/RUNNING 可推进（直接函数调用，不经过 HTTP 层）。"""
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    for _ in range(15):
        scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)


def _db_exec(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        result = conn.execute(text(sql))
        conn.commit()
        rows = result.returns_rows
        if not rows:
            return []
        return [tuple(row) for row in result.all()]


def test_task_continues_after_logout_and_new_session_reads(ctx: tuple[TestClient, Path]) -> None:
    """logout 不中断后台执行；重新登录后任务与卡片可读。"""
    client, db_path = ctx
    user = auth_headers(client)
    task_id, _deck_id = _create_task_before_executor(client, user, db_path)

    # logout 撤销当前 session（204）
    assert client.post("/auth/logout", headers={**user, **_idem()}).status_code == 204
    assert client.get("/auth/me", headers=user).status_code == 401  # 原 session 已失效

    # 后台执行不依赖 session 有效性：executor 直接函数调用继续推进至 COMPLETED
    _run_executor_until_done(db_path)

    # 重新登录（新 session）可读任务与卡片
    login = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "secret-pass-1"}
    )
    assert login.status_code == 200
    new_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    task = client.get(f"/tasks/{task_id}", headers=new_headers)
    assert task.status_code == 200
    assert task.json()["status"] == "COMPLETED"
    assert task.json()["generated_card_count"] == 32


def test_task_continues_after_session_expiry(ctx: tuple[TestClient, Path]) -> None:
    """session 过期（DB 直改 expires_at）不中断后台；新登录可读。"""
    client, db_path = ctx
    user = auth_headers(client)
    task_id, _deck_id = _create_task_before_executor(client, user, db_path)

    # 模拟 30 天绝对有效期到期：直接回拨 expires_at
    _db_exec(
        db_path,
        "UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00Z' WHERE revoked_at IS NULL",
    )
    assert client.get("/auth/me", headers=user).status_code == 401  # 过期 → 401

    _run_executor_until_done(db_path)

    login = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "secret-pass-1"}
    )
    assert login.status_code == 200
    new_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get(f"/tasks/{task_id}", headers=new_headers).json()["status"] == "COMPLETED"


def test_executor_source_has_no_session_dependency() -> None:
    """代码级判别：后台链路源码不含 Authorization/principal/request.state（防未来回归）。"""
    banned = ("Authorization", "Bearer", "principal", "request.state", "auth_sessions")
    roots = [
        REPO_ROOT / "main" / "services" / "tasks",
        REPO_ROOT / "main" / "services" / "generation",
    ]
    hits: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            src = path.read_text()
            for line_no, line in enumerate(src.splitlines(), start=1):
                if any(b in line for b in banned):
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{line_no}: {line.strip()}")
    assert hits == [], "后台链路出现 session/token 依赖:\n" + "\n".join(hits)


def test_operation_key_task_domain_and_ledger_idempotent(ctx: tuple[TestClient, Path]) -> None:
    """operation_key 纯任务域（不含 user/session 维度）；重复扫描账本行数守恒（CAS 不依赖 session）。"""
    client, db_path = ctx
    user = auth_headers(client)
    _task_id, _deck_id = _create_task_before_executor(client, user, db_path)

    _run_executor_until_done(db_path)
    rows = _db_exec(db_path, "SELECT operation_key, user_id FROM llm_call_attempts")
    assert rows, "账本应有调用行"
    # operation_key 纯任务域（planning:{chapter_id}:{gi} / generating:{batch_id} /
    # scoring:{group_key}），不含 user/session 维度（会话轮换不使账本恢复失效）
    stage_prefixes = ("planning:", "generating:", "scoring:", "sample:")
    op_keys = [str(r[0] or "") for r in rows]
    assert all(key.startswith(stage_prefixes) for key in op_keys)
    assert all("user" not in key and "session" not in key for key in op_keys)
    # 归属判别：账本行 user_id 非空（= task.user_id）
    task_user = _db_exec(db_path, "SELECT user_id FROM tasks LIMIT 1")[0][0]
    assert all(r[1] == task_user for r in rows), "账本行 user_id 应等于 task.user_id"

    # 再次扫描：账本行数守恒（状态机/CAS 幂等，不依赖 session）
    count_before = len(rows)
    _run_executor_until_done(db_path)
    count_after = len(_db_exec(db_path, "SELECT 1 FROM llm_call_attempts"))
    assert count_after == count_before


def test_cross_user_task_404_and_observability_isolated(ctx: tuple[TestClient, Path]) -> None:
    """跨用户 ledger/task 404；quality-summary 只含本用户数据。"""
    client, db_path = ctx
    user1 = auth_headers(client)
    task_id, _deck_id = _create_task_before_executor(client, user1, db_path)
    _run_executor_until_done(db_path)

    user2 = auth_headers(client, username="bob2", password="secret-pass-2")
    assert client.get(f"/tasks/{task_id}", headers=user2).status_code == 404
    # 观测聚合：user2 查询不含 user1 数据（窗口内任务完成率 0 或空结构——断言无 user1 的
    # task 出现；简洁口径：响应不含 user1 的 task_id）
    summary = client.get("/observability/quality-summary", headers=user2)
    assert summary.status_code == 200
    assert task_id not in json.dumps(summary.json())


def test_metrics_endpoint_has_no_identity(ctx: tuple[TestClient, Path]) -> None:
    """指标输出无身份聚合（不含 user_id/username/session_id 字样；拉取豁免仅测试 fixture）。"""
    client, _db_path = ctx
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for banned in ("user_id", "username", "session_id"):
        assert banned not in body, f"/metrics 出现身份字段 {banned}"
