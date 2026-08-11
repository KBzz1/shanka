"""验收测试：AC-05 任务恢复与幂等（PRD 9；迁移 schema + HTTP + mock transport + 崩溃模拟）。

映射（PRD AC-05 四条）：
AC-05-a 中断后已入库卡保留 → 批 2 前 SystemExit 崩溃（T1 模式）后：任务停留 RUNNING、
        批 1 SUCCEEDED + 3 卡落库（批次事务粒度——崩溃不丢已完成批次）
AC-05-b 继续任务从游标继续 → 新 app（重启模拟）resume（孤儿 RUNNING 抢占）→ 只处理
        批 2 → COMPLETED；completed_batch_count 1 → 2（游标原子推进）
AC-05-c 已完成批次不重复执行 → 恢复后批 1 仍 SUCCEEDED（retry_count=0），总 chat 调用
        = 3（崩溃 2 次 + 恢复 1 次——批 1 未重跑）
AC-05-d generation_item_id 不重复入库 → 批 2 响应含批内重复内容（q3 出现两次）→ 仅入库
        1 张（duplicate_rate 0.5 观测），全任务卡 generation_item_id 互异
场景 2（取消保留）：任务运行中 cancel → CANCELLED + 已入库卡保留（3 卡不动），取消后不再处理

后台循环间隔拉大（3600s）隔离：显式调 executor.scan_once（test_tasks_api 同款"显式
scan_once"模式）；崩溃模拟 = mock transport 指定调用抛 SystemExit（BaseException——绕过
executor 的 except Exception，等价进程崩溃）。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Batch, Card, Chapter, Device, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.tasks.executor import scan_once as scan_tasks

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Settings]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径 + 应用 settings。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac05.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path, settings


def _uuid() -> str:
    return str(uuid.uuid4())


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_context(db_path: Path, *, device_id: str) -> dict[str, object]:
    """devices 前置 + PDF(PARSED) + 2 章节 + 牌组 + 真实加密 Key（executor 解密路径）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            device_id=device_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
        session.flush()
        chapter_ids: list[str] = []
        for i in range(2):
            ch = Chapter(
                chapter_id=_uuid(),
                file_id=pdf.file_id,
                name=f"第{i + 1}章",
                start_page=i + 1,
                end_page=i + 2,
            )
            session.add(ch)
            session.flush()
            chapter_ids.append(ch.chapter_id)
        session.add(
            ApiKey(
                device_id=device_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        session.commit()
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _payload(seed: dict[str, object]) -> dict[str, object]:
    return {
        "file_id": seed["file_id"],
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "quantity_tendency": "COMPACT",  # 3 知识点/章 × 2 章 = 6 → 2 批（batch_size 3）
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
    }


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _valid_cards(n: int = 3) -> list[dict[str, object]]:
    return [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]


def _cards_response(cards: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}],
            "model": "deepseek-v4-flash",
        },
    )


def _scripted_factory(
    calls: dict[str, int],
    *,
    first_cards: list[dict[str, object]],
    later_cards: list[dict[str, object]],
    crash_call: int = 2,
) -> Callable[[str], DeepSeekClient]:
    """mock transport 工厂：crash_call 次调用抛 SystemExit（崩溃模拟），此前 first_cards、此后 later_cards。

    崩溃后新 client（重启模拟）从 crash_call + 1 次调用继续（同一 calls 计数）。
    """

    def factory(api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == crash_call:
                raise SystemExit("模拟崩溃：批 2 处理中断")
            cards = first_cards if calls["n"] < crash_call else later_cards
            return _cards_response(cards)

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    return factory


def _batches(session: Session, task_id: str) -> list[Batch]:
    return list(
        session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
    )


def test_acceptance_ac05_crash_resume_cursor_and_dedup(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """AC-05 a-d：批 2 前崩溃 → 卡保留 + resume 孤儿从游标继续 + 批 1 不重跑 + generation_item_id 防重。"""
    client, db_path, settings = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # 崩溃模拟（T1 模式）：批 1 一次 chat 成功，批 2 前 SystemExit（绕过 executor 的 except Exception）
    calls: dict[str, int] = {"n": 0}
    factory = _scripted_factory(
        calls,
        first_cards=_valid_cards(3),  # 批 1：q0/q1/q2
        later_cards=[  # 批 2：q3 重复一次 + q4（AC-05-d 防重观测）
            {"type": "QUESTION", "question": "q3", "answer": "a3"},
            {"type": "QUESTION", "question": "q3", "answer": "a3"},
            {"type": "QUESTION", "question": "q4", "answer": "a4"},
        ],
    )
    with pytest.raises(SystemExit):
        scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert calls["n"] == 2  # 批 1 一次 chat、批 2 崩溃

    # AC-05-a：崩溃后任务停留 RUNNING + 批 1 SUCCEEDED + 卡保留（批次事务粒度已落库）
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "RUNNING"
    assert body["generated_card_count"] == 3
    assert body["completed_batch_count"] == 1 and body["total_batch_count"] == 2
    with _db_factory(db_path)() as session:
        task = session.get(Task, task_id)
        assert task is not None
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert [b.status for b in batches] == ["SUCCEEDED", "PENDING"]  # 批 2 claim 随崩溃回滚
    assert len(cards) == 3  # 已入库卡保留（AC-05-a）

    # 新鲜 RUNNING（心跳内）resume → 409（孤儿判据生效，非 PAUSED 路径）
    resp = client.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    # 模拟 30 分钟孤儿窗口流逝（心跳超时）：updated_at 回拨到足够过去（孤儿判据 = 心跳超时）
    with _db_factory(db_path)() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.updated_at = "2026-07-01T00:00:00.000Z"
        session.commit()

    # 新 app（重启模拟）→ resume（孤儿）→ 200 RUNNING
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/tasks/{task_id}", headers=device).json()["status"] == "RUNNING"
        resp = restarted.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
        assert resp.status_code == 200
        assert resp.json()["status"] == "RUNNING"

    # AC-05-b：恢复后从游标继续——只处理批 2 → COMPLETED（同一 transport 继续第 3 次调用）
    n = scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert n == 1
    assert calls["n"] == 3  # AC-05-c：批 1 未重跑（重跑会再增加一次 chat 调用）

    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 5  # 3（批 1）+ 2（批 2：q3 防重跳过 1 张）
    assert body["completed_batch_count"] == 2 and body["total_batch_count"] == 2  # 游标到终值
    with _db_factory(db_path)() as session:
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert [b.status for b in batches] == ["SUCCEEDED", "SUCCEEDED"]
    assert all(b.retry_count == 0 for b in batches)  # 批 1 未重跑、批 2 一次成功
    assert len(cards) == 5
    assert all(c.generation_item_id is not None for c in cards)
    assert len({c.generation_item_id for c in cards}) == len(cards)  # AC-05-d：无重复入库

    # AC-05-d 附证（批次视图观测）：批 2 重复内容被防重跳过 → 只入库 2 张 + 重复率 0.5
    items = client.get(f"/tasks/{task_id}/batches", headers=device).json()["items"]
    gen_batch1 = items[0]["generated_item_ids"]
    gen_batch2 = items[1]["generated_item_ids"]
    assert isinstance(gen_batch1, list) and len(gen_batch1) == 3
    assert isinstance(gen_batch2, list) and len(gen_batch2) == 2
    assert items[1]["duplicate_rate"] == 0.5


def test_acceptance_ac05_cancel_keeps_inserted_cards(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """场景 2（取消保留）：任务运行中 cancel → CANCELLED + 已入库卡保留，取消后不再处理。"""
    client, db_path, _ = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # 任务运行中：批 1 成功后崩溃暂停（T1 模式）→ 任务 RUNNING + 3 卡已入库
    calls: dict[str, int] = {"n": 0}
    factory = _scripted_factory(calls, first_cards=_valid_cards(3), later_cards=_valid_cards(3))
    with pytest.raises(SystemExit):
        scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert calls["n"] == 2

    resp = client.post(f"/tasks/{task_id}/cancel", headers={**device, **_idem()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["ended_at"] is not None
    assert body["generated_card_count"] == 3  # 视图仍报告已入库卡（保留）

    # 已入库卡保留：批 1 SUCCEEDED + 3 卡不动（取消不回滚已完成批次）
    with _db_factory(db_path)() as session:
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert [b.status for b in batches] == ["SUCCEEDED", "PENDING"]
    assert len(cards) == 3

    # 取消后不再处理：CANCELLED 不进入扫描（无 RUNNING 任务），无新 chat 调用、卡数不变
    assert scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory) == 0
    assert calls["n"] == 2
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 3
