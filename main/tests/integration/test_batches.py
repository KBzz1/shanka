"""分批生成集成测试：批次状态机/重试/游标/原子推进（真实 SQLite + mock transport）。

T10 起批=单元（spec §7）：每单元一批 + generation_unit_id 外键；账本为重试预算权威
（Batch.retry_count 只是兼容投影）；fake 评分退役 → 评分 5 字段留 NULL 待 SCORING
（brief Step 5：既有批次测试按 T16 前最小修改跑通）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Batch, Card, KnowledgePoint, Task, TextChunk, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.service import create_task

_NOW = "2026-08-11T00:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'batches.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task_with_kps(session: Session, *, user_id: str, n_kps: int = 4) -> str:
    from infra.db.models import ApiKey, Chapter, Device, PdfFile
    from services.decks.service import create_deck

    # FK 前置守卫：users/devices 行必须先存在（engine 级 PRAGMA foreign_keys=ON）
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    if session.get(Device, user_id) is None:  # ApiKey device 域种子（Task 5 前）
        session.add(Device(device_id=user_id, created_at=_NOW))
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    session.add(
        ApiKey(
            device_id=user_id,
            encrypted_key="enc",
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    task = create_task(
        session,
        user_id=user_id,
        device_id=user_id,  # 双头过渡：ApiKey 校验仍 device 域
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            quantity_tendency="COMPACT",
            difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
        ),
        now=_NOW,
    )
    task.device_id = user_id  # 双列过渡：executor Key 查找仍 device 域（Task 5 切换）
    task.status = "RUNNING"
    task.stage = "GENERATING"
    task.updated_at = _NOW
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"<第{pn}页>内容" * 20} for pn in (1, 2)],
        now=_NOW,
    )
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    units = [
        KnowledgePoint(
            knowledge_point_id=str(uuid.uuid4()),
            task_id=task.task_id,
            chapter_id=ch.chapter_id,
            source_chunk_id=chunks[0].chunk_id,  # 兼容投影（spec §3.1）
            topic=f"学习目标{i + 1}",
            priority=i + 1,
            status="PENDING",
            target_difficulty="BASIC",
            card_type="QUESTION",
            source_chunk_ids=json.dumps([c.chunk_id for c in chunks], ensure_ascii=False),
        )
        for i in range(n_kps)
    ]
    session.add_all(units)
    session.commit()
    return task.task_id


def _valid_cards_json() -> str:
    # T10 批=单元：每单元恰好 1 张锚定类型卡（generator-output schema v2 maxItems=1）
    return json.dumps({"cards": [{"type": "QUESTION", "question": "q", "answer": "a"}]})


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
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, user_id=user)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, generation_units=kps, now=_NOW)
        session.commit()
        total_batches = len(session.scalars(select(Batch).where(Batch.task_id == task_id)).all())
    assert total_batches == len(kps)  # T10：1 单元 = 1 批
    client = _client_ok(session_factory)
    with session_factory() as session:
        session.info["settings"] = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
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
    assert len(cards) == total_batches  # 每单元 1 卡
    assert all(c.source == "GENERATED" for c in cards)
    # T10：批=单元 → coverage=0/1；Rubric 评分字段留 NULL 待 SCORING（T11 回写）
    assert all(b.rubric_version == "v2" for b in batches)
    assert all(b.coverage_rate == 1.0 for b in batches)
    assert all(
        c.evidence_score is None
        and c.correctness_score is None
        and c.difficulty_score is None
        and c.learning_value_score is None
        and c.rubric_total_score is None
        for c in cards
    )


def test_batches_failed_batch_skipped_after_retries(session_factory: Callable[[], Session]) -> None:
    """非法输出（Schema 校验失败）→ 重试预算耗尽（1+limit 次尝试）→ SKIPPED，任务继续。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, user_id=user, n_kps=1)
    with session_factory() as session:
        task = session.get(Task, task_id)
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, generation_units=kps, now=_NOW)
        session.commit()

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"cards": [{"type": "QUESTION"}]}'}}]}
        )

    client = DeepSeekClient(
        Settings(api_key_encryption_key="aa" * 32), transport=httpx.MockTransport(bad_handler)
    )
    with session_factory() as session:
        session.info["settings"] = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
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
    assert all(b.retry_count == 3 for b in batches)  # T10：投影 = 账本尝试数（预算权威）
    assert task.completed_batch_count == len(batches)  # SKIPPED 也推进游标


def test_batches_usage_and_versions_recorded(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task_with_kps(session, user_id=user, n_kps=1)
    with session_factory() as session:
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        plan_batches(session, task_id=task_id, generation_units=kps, now=_NOW)
        session.commit()
    client = _client_ok(session_factory)
    with session_factory() as session:
        session.info["settings"] = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
        process_next_batch(session, task_id=task_id, client=client)
        session.commit()
    with session_factory() as session:
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).first()
    assert batch is not None
    assert batch.cache_hit_tokens == 2
    assert batch.cache_miss_tokens == 8
    assert batch.output_tokens == 5
    assert batch.model == "deepseek-v4-flash"
    assert batch.prompt_version == "v3" and batch.schema_version == "v2"
    assert batch.http_status == 200
