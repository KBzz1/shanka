"""V5B 并发/心跳集成测试：批次抢占单执行者/心跳刷新（真实 SQLite + mock transport）。

种子写入真实加密 Key（executor 解密路径）；process_running_tasks 注入
settings + client_factory（mock transport），生产缺省路径不在此验证。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Batch, Card, KnowledgePoint, Task, TextChunk, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.tasks.executor import process_running_tasks

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(
    session: Session,
    *,
    user_id: str,
    quantity_tendency: str = "COMPACT",
    n_units: int | None = None,
) -> str:
    """种子：RUNNING+GENERATING 任务 + 页文本 + 生成单元（锚定难度/卡型/来源页）+
    按单元建批（spec §7 批=单元，generation_unit_id 必填）。

    T8 起 create_task 不再规划知识点（PENDING+PLANNING）；V5B 并发/心跳测试聚焦
    生成路径，直接构造单元与批次绕过规划 worker（规划路径由 test_planning_executor.py
    覆盖）。test 1 直接调 process_next_batch，不经过 executor 的 plan 路径。
    n_units 覆盖单元数（test 1 单批次抢占场景用 1 单元 = 1 批）。
    """
    from infra.db.models import ApiKey, Chapter, PdfFile
    from services.decks.service import create_deck
    from services.pdf.text_chunks import persist_text_chunks
    from services.tasks.service import create_task

    # FK 前置守卫：users 行必须先存在（engine 级 PRAGMA foreign_keys=ON）
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-08-10T00:00:00.000Z",
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-10T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-10T00:00:00.000Z")
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if (
        session.scalar(
            select(ApiKey.user_id).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
        )
        is None
    ):
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now="2026-08-10T00:00:00.000Z",
    )
    task = create_task(
        session,
        user_id=user_id,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            quantity_tendency=quantity_tendency,
            difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
        ),
        now="2026-08-10T00:00:00.000Z",
    )
    task.status = "RUNNING"
    task.stage = "GENERATING"
    task.updated_at = "2026-08-10T00:00:00.000Z"
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    diffs = ["BASIC", "UNDERSTANDING", "APPLICATION"]
    n_kps = (
        n_units if n_units is not None else {"COMPACT": 3, "BALANCED": 6}.get(quantity_tendency, 3)
    )
    kps = [
        KnowledgePoint(
            knowledge_point_id=str(uuid.uuid4()),
            task_id=task.task_id,
            chapter_id=ch.chapter_id,
            source_chunk_id=chunks[0].chunk_id,  # 兼容投影（spec §3.1）
            topic=f"知识点{i + 1}",
            priority=i + 1,
            status="PENDING",
            target_difficulty=diffs[i % len(diffs)],
            card_type="QUESTION",
            source_chunk_ids=json.dumps([c.chunk_id for c in chunks], ensure_ascii=False),
        )
        for i in range(n_kps)
    ]
    session.add_all(kps)
    session.flush()
    plan_batches(
        session, task_id=task.task_id, generation_units=kps, now="2026-08-10T00:00:00.000Z"
    )
    session.commit()
    return task.task_id


def _valid_cards_json(n: int = 1) -> str:
    """每批 1 张合法卡（spec §7：批=单元，generator-output schema v2 maxItems=1）。"""
    cards = [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]
    return json.dumps({"cards": cards}, ensure_ascii=False)


def _scoring_content(request: httpx.Request) -> str:
    """<SCORING_INPUT> 提取 items → ID 守恒的确定性分数（总分代码计算 9）。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0])
    return json.dumps(
        {
            "scores": [
                {
                    "generation_item_id": item["generation_item_id"],
                    "evidence_score": 2,
                    "correctness_score": 3,
                    "difficulty_score": 2,
                    "learning_value_score": 2,
                }
                for item in payload["items"]
            ]
        },
        ensure_ascii=False,
    )


def _client() -> DeepSeekClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        content = _scoring_content(request) if "<SCORING_INPUT>" in user else _valid_cards_json()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def _client_factory(api_key: str) -> DeepSeekClient:
    return _client()


def test_concurrency_two_workers_single_effect(session_factory: Callable[[], Session]) -> None:
    """两 worker 并发处理同任务：批次条件更新抢占 → 单执行者（无双处理）。

    1 单元 = 1 批（spec §7）：worker A 抢占唯一批次并完成后，worker B 无
    PENDING/FAILED 批次可取 → 0（旧 batch_size 语义下 3 知识点 = 1 批同构）。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)
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
    """心跳：每批完成后 task.updated_at 刷新（seed 时间取安全过去值——与真实服务端时钟可观测比较）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)
        seeded = session.get(Task, task_id)
        assert seeded is not None
        created_at = seeded.created_at
    with session_factory() as session:
        process_running_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
    assert task is not None
    assert task.updated_at is not None and created_at is not None
    assert task.updated_at > created_at  # 心跳刷新（批处理后时间推进）
    assert task.status == "COMPLETED"


def test_concurrency_batch_commit_survives_crash(
    session_factory: Callable[[], Session],
) -> None:
    """批次事务粒度：批 2 处理中崩溃（SystemExit）→ 批 1 已落库（卡+心跳+SUCCEEDED）。

    崩溃模拟：mock transport 第 2 次 chat 抛 SystemExit（BaseException——绕过 executor 的
    except Exception）→ 批 2 的 claim/STARTED 已随调用前事务提交（spec §9），批次保持
    PROCESSING、任务保持 RUNNING；批 1 的批次状态+游标+心跳已随批次事务提交
    （与单次最终 commit 的差异点——Task 3 恢复语义：已完成批次保留、未完成批次经
    心跳超时孤儿恢复后重试）。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, quantity_tendency="BALANCED")
    with session_factory() as session:
        seeded = session.get(Task, task_id)
        assert seeded is not None
        created_at = seeded.created_at
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit("模拟崩溃：批 2 处理中断")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    crashed = False
    with session_factory() as session:
        try:
            process_running_tasks(session, settings=_SETTINGS, client_factory=lambda _k: client)
        except SystemExit:
            crashed = True
            session.rollback()  # 崩溃连接释放写锁（等价于进程死亡）
    assert crashed
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        batches = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert calls == 2  # 批 1 成功、批 2 崩溃
    assert task.status == "RUNNING"  # 终态未落库（崩溃发生在 COMPLETED 之前）
    assert task.updated_at is not None and created_at is not None
    assert task.updated_at > created_at  # 批 1 心跳已随批提交
    # 批 1 已提交；批 2 抢占+STARTED 已提交（spec §9 调用前占位）→ PROCESSING 保留
    # （恢复语义：心跳超时孤儿恢复将其复位 FAILED 后按账本重试——test_ledger 同款）
    assert [b.status for b in batches] == [
        "SUCCEEDED",
        "PROCESSING",
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
    ]
    assert len(cards) == 1  # 批 1 卡片已提交（崩溃不丢已完成批次）
