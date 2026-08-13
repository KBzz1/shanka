"""V6 重写并发/隔离集成测试：同卡后写覆盖、重写×复习串行终态、来源不出响应。

SQLite 单写者语义（BEGIN IMMEDIATE）：同一时刻仅一个写事务，并发写请求被串行化
（并发线程只会得到相同串行结果，且易触发 database is locked 噪音）。故本文件用
**确定性顺序提交**模拟两个 session 的提交序列，断言「后提交覆盖先提交（后写赢）」
的终态一致、无半覆盖——不启动真并发线程（V5B test_concurrency 同款思路：
SQLite 下并发即串行，测试直接验证串行语义）。

重写×复习同理由两条用例覆盖两种先后顺序，断言终态 = 串行执行结果之一，
无半覆盖（内容与排程同一版本来源）。来源不出响应为红线 4/PRD 5.13 守卫断言。
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
from infra.db.models import ApiKey, Base, Card, Device, ReviewEvent, ReviewState, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.cards.rewrite import rewrite_card
from services.cards.service import card_view
from services.decks.service import create_deck
from services.review.service import submit_review

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_DEVICE = "dev"
_USER = "user-1"
_NOW = "2026-08-11T00:00:00.000Z"
_T1 = "2026-08-11T01:00:00.000Z"
_T2 = "2026-08-11T02:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'rewrite-concurrency.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_card(session: Session) -> Card:
    """user 域 GENERATED 卡 + 非初始 ReviewState + AVAILABLE Key（重写前状态）。"""
    session.add(
        User(user_id=_USER, username="u-1", password_hash="x", created_at=_NOW, updated_at=_NOW)
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    session.add(Device(device_id=_DEVICE, created_at=_NOW))
    session.flush()
    session.add(
        ApiKey(
            device_id=_DEVICE,
            encrypted_key=_ENCRYPTED_TEST_KEY,
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, user_id=_USER, name="D", now=_NOW)
    session.flush()
    card = Card(
        card_id=_uuid(),
        deck_id=deck.deck_id,
        user_id=_USER,
        source="GENERATED",
        position=1,
        front="旧正面",
        back="旧背面",
        code="A1",
        card_type="QUESTION",
        question="旧问题？",
        answer="旧答案",
        generation_item_id="gen-old-0000",
        target_difficulty="APPLICATION",
        knowledge_point_ids='["kp-1"]',
        evidence_score=1,
        version="v3",
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(card)
    session.flush()
    session.add(
        ReviewState(
            review_state_id=_uuid(),
            card_id=card.card_id,
            state="REVIEW",
            stability=0.5,
            difficulty=3.0,
            due="2026-08-12T00:00:00.000Z",
            last_review="2026-08-10T00:00:00.000Z",
            reps=5,
            lapses=2,
            last_rating="GOOD",
            updated_at=_NOW,
        )
    )
    session.commit()
    return card


def _rewrite_json(front: str, back: str) -> str:
    """单张合法 QUESTION 卡（front/back 由 question/answer 派生）。"""
    return json.dumps(
        {"cards": [{"type": "QUESTION", "question": front, "answer": back}]},
        ensure_ascii=False,
    )


def _client_returning(content: str) -> DeepSeekClient:
    """mock transport client（重写响应固定为 content；不触网——红线 4 假数据）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def test_rewrite_concurrency_same_card_last_write_wins(
    session_factory: Callable[[], Session],
) -> None:
    """同卡并发重写（不同幂等键请求、两个 session 交替提交）：BEGIN IMMEDIATE 单写者
    串行化——后提交覆盖先提交（后写赢）；终态 = 后写者全量内容（front/back/question/
    answer/generation_item_id 同一版本来源），version 递增、ReviewState 为后写者重置，
    无半覆盖。

    幂等键为 handler 层概念（execute_idempotent），service 层以下以两次独立调用 +
    顺序提交模拟两个不同幂等键请求的串行提交序列（真并发线程在 SQLite 单写者下
    只会得到相同串行结果，见模块 docstring）。
    """
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        first = rewrite_card(
            session,
            user_id=_USER,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_T1,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client_returning(
                _rewrite_json("第一写者问题", "第一写者答案")
            ),
        )
        a = (first.front, first.back, first.question, first.answer, first.generation_item_id)
        session.commit()
    with session_factory() as session:
        second = rewrite_card(
            session,
            user_id=_USER,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_T2,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client_returning(
                _rewrite_json("第二写者问题", "第二写者答案")
            ),
        )
        b = (second.front, second.back, second.question, second.answer, second.generation_item_id)
        session.commit()
    assert a[0] == "第一写者问题"
    assert b[0] == "第二写者问题"  # 两写者各按自己内容提交
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        # 终态 = 后写者（B）全量内容，无半覆盖（内容与标识同一版本来源）
        assert (stored.front, stored.back) == ("第二写者问题", "第二写者答案")
        assert (stored.question, stored.answer) == ("第二写者问题", "第二写者答案")
        assert stored.generation_item_id == b[4]  # 标识 = B 的（旧标识随覆盖作废）
        assert stored.generation_item_id != a[4]
        assert stored.version == "v5"  # v3 → v4 → v5，每次提交递增
        assert stored.updated_at == _T2
        # ReviewState 终态 = 后写者重置（先写者提交的排程无残留）
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0
        assert rs.due == _T2
        assert rs.reps == 0
        assert rs.lapses == 0
        assert rs.last_review is None
        assert rs.last_rating is None
        assert rs.updated_at == _T2


def test_rewrite_concurrency_review_then_rewrite_resets_schedule(
    session_factory: Callable[[], Session],
) -> None:
    """重写×复习并发（顺序一：先复习后重写）：review 先更新排程（REVIEW 态 GOOD → reps 6），
    rewrite 后重置 → 终态 = 重写的新建卡初始排程（NEW/0.0/1.0/due=重写时点），
    复习更新无残留——无半覆盖（终态 = 串行执行结果）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        reviewed = submit_review(
            session,
            user_id=_USER,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now=_T1,
        )
        session.commit()
    assert reviewed["state"] == "REVIEW"  # REVIEW 态卡 GOOD → 保持 REVIEW
    assert reviewed["reps"] == 6  # 5 → 6：复习已更新排程（后写者之前）
    with session_factory() as session:
        rewrite_card(
            session,
            user_id=_USER,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_T2,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client_returning(
                _rewrite_json("重写后问题", "重写后答案")
            ),
        )
        session.commit()
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert stored.front == "重写后问题"
        assert stored.version == "v4"
        # 终态 = 重写的新建卡初始排程（复习更新无残留）
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0
        assert rs.due == _T2
        assert rs.reps == 0
        assert rs.lapses == 0
        assert rs.last_review is None
        assert rs.last_rating is None


def test_rewrite_concurrency_rewrite_then_review_schedules_new_content(
    session_factory: Callable[[], Session],
) -> None:
    """重写×复习并发（顺序二：先重写后复习）：rewrite 重置为 NEW 后，review 基于
    重写后内容与新排程重新调度（NEW 首 GOOD → LEARNING，due=now+10m，reps=1）——
    终态 = 串行执行结果，无半覆盖（排程与内容同一版本来源）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        rewrite_card(
            session,
            user_id=_USER,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_T1,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client_returning(
                _rewrite_json("重写后问题", "重写后答案")
            ),
        )
        session.commit()
    with session_factory() as session:
        reviewed = submit_review(
            session,
            user_id=_USER,
            card_id=card_id,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone="Asia/Shanghai",
            now=_T2,
        )
        session.commit()
    assert reviewed["state"] == "LEARNING"  # NEW 初始卡首 GOOD → Learning（5.2 第 1 行）
    assert reviewed["reps"] == 1  # 从 0 重新计数（重写重置后）
    assert reviewed["due"] == "2026-08-11T02:10:00.000Z"  # now+10m 第一步
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert stored.front == "重写后问题"  # 复习基于重写后内容（内容与排程一致）
        assert rs.state == "LEARNING"
        assert rs.last_review == _T2
        assert rs.reps == 1
        assert rs.lapses == 0
        ev = session.scalar(select(ReviewEvent).where(ReviewEvent.card_id == card_id))
        assert ev is not None  # 复习事件引用同一张卡（无半覆盖：事件与状态同源）
        assert ev.rating == "GOOD"


def test_rewrite_concurrency_response_no_pdf_source_fields(
    session_factory: Callable[[], Session],
) -> None:
    """来源不出响应（红线 4/PRD 5.13）：重写响应 JSON 不含 storage_key/file_id/pdf
    相关字段——PDF 来源信息不随卡片响应泄漏（响应仅 card_view 契约字段）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        card = rewrite_card(
            session,
            user_id=_USER,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_T1,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client_returning(_rewrite_json("新问题", "新答案")),
        )
        body = json.dumps(card_view(card), ensure_ascii=False)
    lowered = body.lower()
    for forbidden in ("storage_key", "file_id", "pdf"):
        assert forbidden not in lowered, f"重写响应不得包含来源字段: {forbidden}"
