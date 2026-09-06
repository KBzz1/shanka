"""R25-07 HTTP 层并发写压力：生成推进与用户写并发，SQLite 单写者下零锁死。

风险（Progress R25-07）：生成长事务可能阻塞撤销/设置等用户写入。缓解设计
（LLM 调用移出写事务、批次短事务、租约 CAS）已有 service 级验证
（test_concurrency_llm_call_holds_no_write_transaction 等）；本文件补 HTTP 端到端：
后台线程推进同用户 GENERATING 任务的批次写（claim/STARTED 账本/STAGED 卡/发布/心跳），
同时三个 HTTP 线程并发混合用户写（评分/偏好/牌组改名），断言全部响应 2xx
（无 database is locked → 500 INTERNAL_ERROR）且任务正常收敛终态。
"""

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine, create_session_factory
from tests.conftest import auth_headers
from tests.integration.test_concurrency import (
    _SETTINGS,
    _client_factory,
    _seed_task,
)

_ROUNDS = 20


def _upgrade(db_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.mark.pressure
def test_http_write_pressure_generation_and_user_writes_concurrent(tmp_path: Path) -> None:
    db_path = tmp_path / "pressure.db"
    _upgrade(db_path)
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
        rate_limit_ip_burst=1000,
        rate_limit_write_per_minute=100000,
    )
    app = create_app(settings)
    client = TestClient(app)

    # HTTP 用户（评分/偏好/牌组写主体）与生成任务同属一个 user_id
    headers = auth_headers(client, username="pressure", password="secret-pass-1")
    engine = create_db_engine(f"sqlite:///{db_path}")
    user_id = (
        engine.connect()
        .execute(text("SELECT user_id FROM users WHERE username = 'pressure'"))
        .scalar_one()
    )
    factory = create_session_factory(engine)

    def _write_headers() -> dict[str, str]:
        return {**headers, "Idempotency-Key": str(uuid.uuid4())}

    deck_id = client.post("/decks", json={"name": "压测牌组"}, headers=_write_headers()).json()[
        "deck_id"
    ]
    card_ids = [
        client.post(
            f"/decks/{deck_id}/cards",
            json={"front": f"卡{i}", "back": "背"},
            headers=_write_headers(),
        ).json()["card_id"]
        for i in range(2)
    ]
    with factory() as session:
        task_id = _seed_task(session, user_id=user_id, n_units=8)

    outcomes: list[tuple[str, int, str]] = []

    def generation_worker() -> None:
        """逐轮扫描推进（每轮独立短事务：批次粒度 commit，模拟进程内 executor 循环）。"""
        from services.tasks.executor import process_active_tasks

        while True:
            with factory() as session:
                processed = process_active_tasks(
                    session, settings=_SETTINGS, client_factory=_client_factory
                )
                session.commit()
            if processed == 0:
                return

    def writer_worker(mode: str) -> None:
        writer_client = TestClient(app)
        for i in range(_ROUNDS):
            if mode == "review":
                r = writer_client.post(
                    "/review-events",
                    json={
                        "card_id": card_ids[i % 2],
                        "rating": "GOOD",
                        "client_event_id": str(uuid.uuid4()),
                    },
                    headers=_write_headers(),
                )
            elif mode == "preferences":
                r = writer_client.patch(
                    "/preferences",
                    json={"daily_learning_goal": 40 if i % 2 == 0 else 50},
                    headers=_write_headers(),
                )
            else:
                r = writer_client.patch(
                    f"/decks/{deck_id}", json={"name": f"改名{i}"}, headers=_write_headers()
                )
            code = r.json().get("error", {}).get("code", "") if r.status_code >= 400 else ""
            outcomes.append((mode, r.status_code, code))

    with ThreadPoolExecutor(max_workers=4) as pool:
        gen = pool.submit(generation_worker)
        writers = [pool.submit(writer_worker, m) for m in ("review", "preferences", "deck")]
        for w in writers:
            w.result()
        gen.result()

    assert len(outcomes) == _ROUNDS * 3
    failures = [(m, s, c) for m, s, c in outcomes if s >= 400]
    assert not failures, f"并发用户写出现非 2xx（SQLite 锁死/状态冲突）: {failures[:5]}"
    with factory() as session:
        status = session.execute(
            text("SELECT status FROM tasks WHERE task_id = :t"), {"t": task_id}
        ).scalar_one()
    assert status == "COMPLETED", f"生成线程未收敛终态: {status}"
    # 撤销写路径也纳入压力面：删除批次进入 + 撤销（与评分后的卡并发无冲突）
    batch = client.delete(f"/cards/{card_ids[0]}", headers=_write_headers()).json()
    undone = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers=_write_headers()
    )
    assert undone.status_code == 200
