"""STAGED 生成与原子发布集成测试（Task 6；structure-contract 3.9/4.1/4.2）。

两组 RED 用例：
- 统一可见谓词（3.9）：普通 card/deck/study/stats 查询必须经共享谓词排除
  `STAGED` 与 `delete_batch_id IS NOT NULL` 卡——列表/进度聚合/到期队列/统计/
  单卡操作全口径（禁止各模块自行拼条件或漏写）。
- 失败注入（4.1 零部分可见）：规划/生成/校验/持久化/发布任一点失败 → 任务
  FAILED 且用户侧零可见卡；0 张有效卡 → TASK_ZERO_CARDS 整体失败（V25-D-23）。
  批次级失败（Schema 重试达上限 → SKIPPED）不置 FAILED（4.2），但整任务
  无有效卡时发布阶段整体失败。

种子写入真实加密 Key（executor 解密路径）；process_active_tasks 注入
settings + client_factory（mock transport），生产缺省路径不在此验证。
"""

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from domain.card import VISIBLE_PREDICATE_SQL as _VISIBLE_PREDICATE_SQL
from infra.db.models import (
    ApiKey,
    Base,
    Batch,
    Card,
    CardDeletionBatch,
    Chapter,
    Deck,
    KnowledgePoint,
    LearningProject,
    PdfFile,
    ReviewEvent,
    ReviewState,
    Task,
    TextChunk,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.cards.deletion import mark_card_deleted
from services.cards.rewrite import rewrite_card
from services.cards.service import list_cards, update_card
from services.decks.service import deck_progress
from services.generation.batches import plan_batches
from services.pdf.text_chunks import persist_text_chunks
from services.review.service import review_queue, submit_review
from services.stats.service import dashboard
from services.tasks.executor import process_active_tasks
from services.tasks.service import create_task

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_NOW = "2026-08-15T00:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'atomic_publish.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------- 种子：用户 + 牌组 + 卡（可见谓词组） ----------


def _seed_user_deck(session: Session, *, user_id: str) -> dict[str, str]:
    """users + 手动牌组（可见谓词测试基座）。"""
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()
    deck = _seed_deck(session, user_id=user_id)
    return {"deck_id": deck.deck_id}


def _seed_deck(session: Session, *, user_id: str) -> Deck:
    deck = Deck(
        deck_id=_uuid(),
        user_id=user_id,
        name="D",
        source="MANUAL",
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(deck)
    session.flush()
    return deck


def _seed_deletion_batch(session: Session, *, user_id: str) -> str:
    """card_deletion_batches 行（delete_batch_id FK 前置；Task 8 前测试直接构造）。"""
    batch = CardDeletionBatch(
        delete_batch_id=_uuid(),
        user_id=user_id,
        status="PENDING",
        undo_until=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(batch)
    session.flush()
    return batch.delete_batch_id


def _seed_card(
    session: Session,
    *,
    deck_id: str,
    user_id: str,
    position: int,
    publication_state: str = "PUBLISHED",
    delete_batch_id: str | None = None,
    rs_state: str = "NEW",
    rs_stability: float = 0.0,
    due: str = _NOW,
    reviewed: bool = False,
) -> Card:
    """卡 + ReviewState（+ 可选 ReviewEvent）同事务种子（默认可见 PUBLISHED 卡）。"""
    card = Card(
        card_id=_uuid(),
        deck_id=deck_id,
        user_id=user_id,
        source="GENERATED",
        position=position,
        front=f"f{position}",
        back=f"b{position}",
        card_type="QUESTION",
        question=f"q{position}",
        answer=f"a{position}",
        publication_state=publication_state,
        delete_batch_id=delete_batch_id,
        version="v1",
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(card)
    session.flush()
    session.add(
        ReviewState(
            review_state_id=_uuid(),
            card_id=card.card_id,
            state=rs_state,
            stability=rs_stability,
            difficulty=1.0,
            due=due,
            reps=0,
            lapses=0,
            updated_at=_NOW,
        )
    )
    if reviewed:
        session.add(
            ReviewEvent(
                review_event_id=_uuid(),
                user_id=user_id,
                card_id=card.card_id,
                client_event_id=_uuid(),
                rating="GOOD",
                reviewed_at=_NOW,
                created_at=_NOW,
            )
        )
    session.flush()
    return card


# ---------- 种子：执行器流水线任务（失败注入组） ----------


def _seed_task(
    session: Session, *, user_id: str, coverage_mode: str = "COMPACT", n_units: int | None = None
) -> str:
    """GENERATING 任务（stage=GENERATING）+ 页文本 + 生成单元 + 按单元建批
    （spec §7 批=单元，generation_unit_id 必填）——跳过样卡/规划直入生成路径。"""
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
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
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    deck = _seed_deck(session, user_id=user_id)
    deck.project_id = project.project_id
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if session.scalar(select(ApiKey.user_id).where(ApiKey.user_id == user_id)) is None:
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now=_NOW,
    )
    task = create_task(
        session,
        user_id=user_id,
        project_id=project.project_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            coverage_mode=coverage_mode,
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        ),
        now=_NOW,
    )
    task.status = "GENERATING"  # V2.5 七态：跳过样卡阶段直入生成（批次语义聚焦）
    task.stage = "GENERATING"
    task.updated_at = _NOW
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    diffs = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]  # V2.5 改名（3.5）
    n_kps = n_units if n_units is not None else {"COMPACT": 3, "BALANCED": 6}.get(coverage_mode, 3)
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
    plan_batches(session, task_id=task.task_id, generation_units=kps, now=_NOW)
    session.commit()
    return task.task_id


def _seed_planning_task(session: Session, *, user_id: str) -> str:
    """GENERATING+PLANNING 任务（start 后状态）+ 章节 + 页文本——规划 worker
    全流程基座（重试替代结果用例：原任务规划/生成阶段失败后 retry 的完整流水线）。"""
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="p.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    deck = _seed_deck(session, user_id=user_id)
    deck.project_id = project.project_id
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if session.scalar(select(ApiKey.user_id).where(ApiKey.user_id == user_id)) is None:
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now=_NOW,
    )
    task = create_task(
        session,
        user_id=user_id,
        project_id=project.project_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            coverage_mode="COMPACT",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        ),
        now=_NOW,
    )
    task.status = "GENERATING"  # start 后状态（4.1）：GENERATING + internal_stage=PLANNING
    task.stage = "PLANNING"
    task.updated_at = _NOW
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


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport 分派：<SCORING_INPUT> → 分数；其余（<GENERATOR_INPUT>）→ 每批 1 卡。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        content = _scoring_content(request) if "<SCORING_INPUT>" in user else _valid_cards_json()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                },
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def _planning_content(request: httpx.Request) -> str:
    """<PLANNER_INPUT> 提取组页 → 1 个合法单元（引用请求内首页）。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0])
    chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
    return json.dumps(
        {
            "units": [
                {
                    "source_chunk_ids": [chunk_ids[0]],
                    "learning_objective": "规划目标",
                    "target_difficulty": "BASIC",
                    "card_type": "QUESTION",
                }
            ]
        },
        ensure_ascii=False,
    )


# ---------- RED 组 A：统一可见谓词（3.9） ----------


def test_visible_predicate_card_list_excludes_staged_and_delete_batch(
    session_factory: Callable[[], Session],
) -> None:
    """普通卡片列表（6.5 GET /decks/{deck_id}/cards）：STAGED 与 delete_batch_id 非空
    卡均不可见——只返回 PUBLISHED 且无删除批次的卡。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_user_deck(session, user_id=user)
        session.flush()
        deletion_batch_id = _seed_deletion_batch(session, user_id=user)
        _seed_card(session, deck_id=ctx["deck_id"], user_id=user, position=1)  # 可见
        _seed_card(
            session, deck_id=ctx["deck_id"], user_id=user, position=2, publication_state="STAGED"
        )
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=3,
            delete_batch_id=deletion_batch_id,
        )
        session.commit()
    with session_factory() as session:
        cards = list_cards(session, user_id=user, deck_id=ctx["deck_id"])
    assert [c.position for c in cards] == [1]
    assert all(c.publication_state == "PUBLISHED" and c.delete_batch_id is None for c in cards)
    # 收敛断言：共享谓词常量即 3.9 原文（所有普通查询复用同一常量，禁止自拼等价条件）
    assert "publication_state = 'PUBLISHED'" in _VISIBLE_PREDICATE_SQL
    assert "delete_batch_id IS NULL" in _VISIBLE_PREDICATE_SQL


def test_visible_predicate_deck_progress_counts_visible_only(
    session_factory: Callable[[], Session],
) -> None:
    """牌组进度聚合（3.8/5.3）：card_count/due_count/mastered/review_count 全部
    只含可见卡（STAGED / delete_batch_id 非空排除）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_user_deck(session, user_id=user)
        session.flush()
        deletion_batch_id = _seed_deletion_batch(session, user_id=user)
        # 可见卡：NEW + REVIEW(stability 21) + 1 条复习事件
        _seed_card(session, deck_id=ctx["deck_id"], user_id=user, position=1)
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=2,
            rs_state="REVIEW",
            rs_stability=30.0,
            reviewed=True,
        )
        # 不可见卡：STAGED（REVIEW 掌握卡 + 复习事件）与删除批次卡（NEW）
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=3,
            publication_state="STAGED",
            rs_state="REVIEW",
            rs_stability=30.0,
            reviewed=True,
        )
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=4,
            delete_batch_id=deletion_batch_id,
            rs_state="REVIEW",
            rs_stability=30.0,
        )
        session.commit()
    with session_factory() as session:
        progress = deck_progress(session, user_id=user, deck_id=ctx["deck_id"], now=_NOW)
    assert progress["card_count"] == 2
    assert progress["due_count"] == 2  # 两张可见卡 due<=now
    assert progress["mastered_card_count"] == 1  # 仅可见 REVIEW 卡
    assert progress["review_count"] == 1  # 仅可见卡事件
    assert progress["mastery_ratio"] == 0.5


def test_visible_predicate_review_queue_excludes_staged(
    session_factory: Callable[[], Session],
) -> None:
    """到期队列（5.15/6.6）：STAGED / delete_batch_id 非空卡不进队列（due<=now 也不可见）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_user_deck(session, user_id=user)
        session.flush()
        deletion_batch_id = _seed_deletion_batch(session, user_id=user)
        _seed_card(session, deck_id=ctx["deck_id"], user_id=user, position=1)  # 可见到期
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=2,
            publication_state="STAGED",
            due="2026-08-10T00:00:00.000Z",
        )
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=3,
            delete_batch_id=deletion_batch_id,
        )
        session.commit()
    with session_factory() as session:
        queue = review_queue(session, user_id=user, deck_id=ctx["deck_id"], now=_NOW)
    assert [q["position"] for q in queue] == [1]


def test_visible_predicate_stats_mastered_excludes_staged(
    session_factory: Callable[[], Session],
) -> None:
    """看板统计（3.12/5.3）：mastered_card_count 只计可见卡（STAGED 掌握卡不计）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_user_deck(session, user_id=user)
        session.flush()
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=1,
            rs_state="REVIEW",
            rs_stability=30.0,
        )
        _seed_card(
            session,
            deck_id=ctx["deck_id"],
            user_id=user,
            position=2,
            publication_state="STAGED",
            rs_state="REVIEW",
            rs_stability=30.0,
        )
        session.commit()
    with session_factory() as session:
        stats = dashboard(
            session,
            user_id=user,
            timezone="Asia/Shanghai",
            weekly_goal=50,
            now=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
        )
    assert stats["mastered_card_count"] == 1
    assert stats["has_data"] is True


def test_visible_predicate_single_card_operations_hidden_for_staged(
    session_factory: Callable[[], Session],
) -> None:
    """单卡用户操作（PATCH/DELETE/重写/评级）对 STAGED 卡统一 CARD_NOT_FOUND——
    不可见卡不存在于用户视野（4.1：STAGED 卡对任何用户侧查询不可见）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed_user_deck(session, user_id=user)
        session.flush()
        staged = _seed_card(
            session, deck_id=ctx["deck_id"], user_id=user, position=1, publication_state="STAGED"
        )
        session.commit()
        staged_id = staged.card_id
    with session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            update_card(session, user_id=user, card_id=staged_id, front="x", back="y", now=_NOW)
        assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        mark_card_deleted(session, user_id=user, card_id=staged_id, delete_batch_id=None, now=_NOW)
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        rewrite_card(
            session,
            user_id=user,
            card_id=staged_id,
            custom_requirements=None,
            now=_NOW,
            settings=_SETTINGS,
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        submit_review(
            session,
            user_id=user,
            card_id=staged_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone=None,
            now=_NOW,
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND


# ---------- RED 组 B：失败注入（4.1 零部分可见） ----------


def test_failure_planning_fails_task_zero_cards(session_factory: Callable[[], Session]) -> None:
    """规划失败（401 Key 失效）：任务 FAILED + failure_stage=PLANNING，0 卡入库
    （规划阶段无卡可隔离——零部分可见恒成立）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_planning_task(session, user_id=user)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.error_code == "API_KEY_UNAVAILABLE"
    assert task.generated_card_count == 0
    assert cards == []  # 规划失败：零卡
    with session_factory() as session:
        visible = list_cards(session, user_id=user, deck_id=str(task.deck_id))
    assert visible == []  # 用户侧零可见


def test_failure_generation_midway_isolates_staged_cards(
    session_factory: Callable[[], Session],
) -> None:
    """生成中途失败（第 2 批 401）：任务 FAILED；已入库卡保持 STAGED 隔离——
    牌组列表零可见（RED：改造前卡直接 PUBLISHED 泄漏部分可见）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, coverage_mode="BALANCED")  # 6 单元
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": _valid_cards_json()}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "model": "deepseek-v4-flash",
                },
            )
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert calls == 2
    assert task.status == "FAILED"
    assert task.error_code == "API_KEY_UNAVAILABLE"
    assert task.failure_stage == "GENERATING"
    assert task.generated_card_count == 0  # 失败任务为 0（3.4：只统计已发布卡）
    assert len(cards) == 1  # 第 1 批卡保留隔离（4.1：已入库卡保留）
    assert cards[0].publication_state == "STAGED"
    with session_factory() as session:
        visible = list_cards(session, user_id=user, deck_id=str(task.deck_id))
    assert visible == []  # 零部分可见


def test_failure_validation_all_batches_skipped_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """输出校验失败（Schema 违约重试达上限 → 全部批次 SKIPPED，4.2 批次级失败）：
    任务无任何有效卡 → 发布阶段整体失败 TASK_ZERO_CARDS（V25-D-23），
    不显示"完成 0 张"（RED：改造前任务 COMPLETED+0）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)  # 3 单元 = 3 批
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # 缺 question/answer：generator-output schema v2 违约 → 重试预算耗尽 → SKIPPED
        content = json.dumps({"cards": [{"type": "QUESTION"}]}, ensure_ascii=False)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek-v4-flash",
            },
        )

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batches = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert calls == 9  # 3 批 × 3 次尝试（1+retry_limit=2）
    assert n == 1
    assert task.status == "FAILED"
    assert task.error_code == "TASK_ZERO_CARDS"
    assert task.failure_stage == "PUBLISHING"
    assert task.generated_card_count == 0
    assert all(b.status == "SKIPPED" for b in batches)  # 批次级失败不置任务 FAILED（4.2）
    assert cards == []
    assert task.completed_batch_count == 3 and task.total_batch_count == 3  # 游标到终值


def test_failure_persistence_crash_isolates_committed_cards(
    session_factory: Callable[[], Session],
) -> None:
    """持久化边界崩溃（批 2 chat 中 SystemExit，BaseException 绕过业务 except）：
    批 1 已提交卡必须保持 STAGED——崩溃中间态不得泄漏部分可见（RED：改造前
    批 1 卡 PUBLISHED 直接可见）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, coverage_mode="BALANCED")
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
            process_active_tasks(session, settings=_SETTINGS, client_factory=lambda _k: client)
        except SystemExit:
            crashed = True
            session.rollback()  # 崩溃连接释放写锁（等价于进程死亡）
    assert crashed
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert task.status == "GENERATING"  # 终态未落库（崩溃发生在 COMPLETED 之前）
    assert len(cards) == 1  # 批 1 卡已提交（批次事务粒度）
    assert cards[0].publication_state == "STAGED"  # 已提交卡隔离
    with session_factory() as session:
        visible = list_cards(session, user_id=user, deck_id=str(task.deck_id))
    assert visible == []  # 崩溃中间态用户侧零可见
    # 恢复：心跳回拨 → 下一轮扫描孤儿恢复 → 全批完成 → 原子发布
    with session_factory() as session:
        task_row = session.get(Task, task_id)
        assert task_row is not None
        task_row.updated_at = "2026-07-01T00:00:00.000Z"  # 30 分钟孤儿窗口流逝
        session.commit()
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert task.generated_card_count == 6
    assert all(c.publication_state == "PUBLISHED" for c in cards)
    with session_factory() as session:
        visible = list_cards(session, user_id=user, deck_id=str(task.deck_id))
    assert len(visible) == 6  # 发布后全量可见


def test_publish_zero_cards_aborts_task(session_factory: Callable[[], Session]) -> None:
    """发布阶段 0 张有效卡（全部批次合法弃权 SOURCE_INSUFFICIENT）：任务 FAILED +
    TASK_ZERO_CARDS（4.1 V25-D-23：0 张有效卡整体失败，不显示"完成 0 张"）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)  # 3 单元 = 3 批
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = json.dumps({"cards": []}, ensure_ascii=False)  # 合法显式空数组 = 安全弃权
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batches = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert calls == 3  # 弃权不重试（每批 1 次）
    assert n == 1
    assert task.status == "FAILED"
    assert task.error_code == "TASK_ZERO_CARDS"
    assert task.failure_stage == "PUBLISHING"
    assert all(b.status == "SKIPPED" for b in batches)
    assert cards == []
    assert task.generated_card_count == 0
