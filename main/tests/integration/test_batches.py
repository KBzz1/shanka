"""分批生成集成测试：批次状态机/重试/游标/原子推进（真实 SQLite + mock transport）。"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from infra.db.models import Base, Batch, Card, KnowledgePoint, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.tasks.service import create_task


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'batches.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task_with_kps(session: Session, *, device_id: str, n_kps: int = 4) -> str:
    from infra.db.models import ApiKey, Chapter, Device, PdfFile
    from services.decks.service import create_deck

    # FK 前置守卫：devices 行必须先存在（engine 级 PRAGMA foreign_keys=ON）
    if session.get(Device, device_id) is None:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    session.add(
        ApiKey(
            device_id=device_id,
            encrypted_key="enc",
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()
    task = create_task(
        session,
        device_id=device_id,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config={
            "quantity_tendency": "COMPACT",
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
        now="2026-08-11T00:00:00.000Z",
    )
    session.commit()
    return task.task_id


def _valid_cards_json(n: int = 2) -> str:
    cards = [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]
    return json.dumps({"cards": cards}, ensure_ascii=False)


def _client_ok(session_factory: Callable[[], Session]) -> DeepSeekClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                },
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(
        Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(handler)
    )


def test_batches_plan_and_process_all(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()
        total_batches = len(session.scalars(select(Batch).where(Batch.task_id == task_id)).all())
    assert total_batches >= 1
    client = _client_ok(session_factory)
    with session_factory() as session:
        processed = 0
        while True:
            n = process_next_batch(session, task_id=task_id, client=client)
            if n == 0:
                break
            session.commit()
            processed += n
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert processed == total_batches
    assert all(b.status == "SUCCEEDED" for b in batches)
    assert task.completed_batch_count == total_batches  # 游标原子推进
    assert task.total_batch_count == total_batches
    assert len(cards) > 0
    assert all(c.source == "GENERATED" for c in cards)
    # T3 审查 carry-forward：SUCCEEDED 批次 Rubric 值断言（AC-04/07；仅观测不影响入库）
    assert all(b.rubric_version == "v1" for b in batches)
    assert all(
        c.evidence_score is not None
        and c.correctness_score is not None
        and c.difficulty_score is not None
        and c.learning_value_score is not None
        and c.rubric_total_score is not None
        and 0 < c.rubric_total_score <= 12
        for c in cards
    )
    assert all(
        b.coverage_rate is not None
        and b.duplicate_rate is not None
        and b.difficulty_distribution is not None
        and b.chapter_distribution is not None
        and b.card_type_distribution is not None
        and b.difficulty_deviation is not None
        for b in batches
    )


def test_batches_failed_batch_skipped_after_retries(session_factory: Callable[[], Session]) -> None:
    """非法输出（Schema 校验失败）→ 重试 2 次 → SKIPPED，任务继续（4.2）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"cards": [{"type": "QUESTION"}]}'}}]}
        )

    client = DeepSeekClient(
        Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(bad_handler)
    )
    with session_factory() as session:
        attempts = 0
        while True:  # 无待处理批次（返回 0）时终止——验证 break 路径
            if process_next_batch(session, task_id=task_id, client=client) == 0:
                break
            attempts += 1
            session.commit()
    with session_factory() as session:
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
        task = session.get(Task, task_id)
    assert task is not None
    assert attempts == 3  # 契约 3.7：最多 2 次重试共 3 次尝试（每批）
    assert all(b.status == "SKIPPED" for b in batches)
    assert all(b.retry_count == 2 for b in batches)  # 重试计数 = 2（3 次尝试）
    assert task.completed_batch_count == len(batches)  # SKIPPED 也推进游标


def test_batches_usage_and_versions_recorded(session_factory: Callable[[], Session]) -> None:
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, device_id=device)
    with session_factory() as session:
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, knowledge_points=kps)
        session.commit()
    client = _client_ok(session_factory)
    with session_factory() as session:
        process_next_batch(session, task_id=task_id, client=client)
        session.commit()
    with session_factory() as session:
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).first()
    assert batch is not None
    assert batch.cache_hit_tokens == 2
    assert batch.cache_miss_tokens == 8
    assert batch.output_tokens == 5
    assert batch.model == "deepseek-v4-flash"
    assert (
        batch.prompt_version == "v2" and batch.schema_version == "v1"
    )  # generator v2（R1 canary 修复）
    assert batch.http_status == 200
