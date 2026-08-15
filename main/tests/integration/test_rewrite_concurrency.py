"""V2.5 重写并发/隔离集成测试：CAS 先应用者胜、apply×复习串行终态、来源不出响应。

两阶段语义（3.19）：预览创建不改卡；apply 是版本 CAS 原子替换——并发两个预览
（不同幂等键请求）各自基于同一 base_card_version，先 apply 者胜，后 apply 者
409 CARD_VERSION_CONFLICT（与 V6 单步「后写覆盖先写」语义相反，CAS 保证只产生
一次有效替换）。SQLite 单写者语义（BEGIN IMMEDIATE）下并发写被串行化，故用
**确定性顺序提交**模拟两个 session 的提交序列，断言「先应用者胜」的终态一致、
无半覆盖（内容与排程同一版本来源）。

apply×复习同理由两条用例覆盖两种先后顺序，断言终态 = 串行执行结果之一，
无半覆盖。来源不出响应为红线 4/PRD 5.13 守卫断言。
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
from app.errors import AppError, ErrorCode
from infra.db.models import (
    ApiKey,
    Base,
    Card,
    CardRewritePreview,
    Chapter,
    PdfFile,
    ReviewEvent,
    ReviewState,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.cards.rewrite import apply_rewrite_preview, create_rewrite_preview, preview_view
from services.cards.service import card_view
from services.decks.service import create_deck
from services.review.service import submit_review

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

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
    """user 域 GENERATED 卡 + 完整来源链 + 非初始 ReviewState + AVAILABLE Key（重写前状态）。"""
    session.add(
        User(
            user_id=_USER,
            username="u-1",
            email="u-1@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    session.execute(
        insert(ApiKey).values(
            user_id=_USER,
            encrypted_key=_ENCRYPTED_TEST_KEY,
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, user_id=_USER, name="D", now=_NOW)
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=_USER,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    task = Task(
        task_id=_uuid(),
        user_id=_USER,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        status="COMPLETED",
        stage=None,
        selected_chapters=json.dumps(
            [{"chapter_id": ch.chapter_id, "start_page": 1, "end_page": 2}]
        ),
        generation_config="{}",
        generated_card_count=1,
        resumable=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(task)
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
        source_task_id=task.task_id,
        chapter_id=ch.chapter_id,
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


def _create_preview(
    session: Session, *, card_id: str, content: str, now: str
) -> CardRewritePreview:
    """创建预览（每个预览一个 client 实例，避免共享 transport 状态）。"""
    preview = create_rewrite_preview(
        session,
        user_id=_USER,
        card_id=card_id,
        custom_requirements=None,
        now=now,
        settings=_SETTINGS,
        client_factory=lambda _api_key: _client_returning(content),
    )
    session.commit()
    return preview


def test_rewrite_concurrency_same_card_first_apply_wins(
    session_factory: Callable[[], Session],
) -> None:
    """同卡并发两预览（不同幂等键请求、两个 session 交替提交）：各自基于同一
    base_card_version=v3——先 apply 者胜（CAS 首写赢，与 V6 单步「后写覆盖」相反），
    后 apply 者 409 CARD_VERSION_CONFLICT 原卡不变；终态 = 先应用者全量内容
    （front/back/question/answer/generation_item_id 同一版本来源），version 递增一次、
    ReviewState 为应用者重置，无半覆盖。只产生一次有效替换（FR-08）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        first = _create_preview(
            session, card_id=card_id, content=_rewrite_json("第一写者问题", "第一写者答案"), now=_T1
        )
        first_rid = first.rewrite_id
    with session_factory() as session:
        second = _create_preview(
            session, card_id=card_id, content=_rewrite_json("第二写者问题", "第二写者答案"), now=_T1
        )
        second_rid = second.rewrite_id
    assert first_rid != second_rid
    with session_factory() as session:
        applied = apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=first_rid, now=_T2
        )
        a = (applied.front, applied.back, applied.generation_item_id)
        session.commit()
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=second_rid, now=_T2
        )
    assert excinfo.value.code is ErrorCode.CARD_VERSION_CONFLICT  # 后 apply 者 CAS 失败
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        # 终态 = 先应用者（A）全量内容，无半覆盖（内容与标识同一版本来源）
        assert (stored.front, stored.back) == ("第一写者问题", "第一写者答案")
        assert (stored.question, stored.answer) == ("第一写者问题", "第一写者答案")
        assert stored.generation_item_id == a[2]  # 标识 = 先应用者
        assert stored.version == "v4"  # v3 → 只递增一次（仅一次有效替换）
        assert stored.updated_at == _T2
        # ReviewState 终态 = 应用者重置
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0
        assert rs.due == _T2
        assert rs.reps == 0
        assert rs.lapses == 0
        assert rs.last_review is None
        assert rs.last_rating is None
        assert rs.updated_at == _T2
        # 预览状态：先应用者 APPLIED，后应用者保持 PENDING（可取消）
        first_row = session.get(CardRewritePreview, first_rid)
        second_row = session.get(CardRewritePreview, second_rid)
        assert first_row is not None and first_row.status == "APPLIED"
        assert second_row is not None and second_row.status == "PENDING"


def test_rewrite_concurrency_review_then_rewrite_resets_schedule(
    session_factory: Callable[[], Session],
) -> None:
    """重写×复习并发（顺序一：先复习后应用）：review 先更新排程（REVIEW 态 GOOD → reps 6），
    apply 后重置 → 终态 = 重写的新建卡初始排程（NEW/0.0/1.0/due=应用时点），
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
    assert reviewed["reps"] == 6  # 5 → 6：复习已更新排程（应用之前）
    with session_factory() as session:
        preview = _create_preview(
            session, card_id=card_id, content=_rewrite_json("重写后问题", "重写后答案"), now=_T2
        )
        apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_T2
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
    """重写×复习并发（顺序二：先应用后复习）：apply 重置为 NEW 后，review 基于
    重写后内容与新排程重新调度（NEW 首 GOOD → LEARNING，due=now+10m，reps=1）——
    终态 = 串行执行结果，无半覆盖（排程与内容同一版本来源）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        preview = _create_preview(
            session, card_id=card_id, content=_rewrite_json("重写后问题", "重写后答案"), now=_T1
        )
        apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_T1
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
    """来源不出响应（红线 4/PRD 5.13）：预览与 apply 响应 JSON 不含 storage_key/file_id/pdf
    相关字段——PDF 来源信息不随卡片响应泄漏（响应仅 card_view/preview_view 契约字段）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session:
        preview = _create_preview(
            session, card_id=card_id, content=_rewrite_json("新问题", "新答案"), now=_T1
        )
        preview_body = json.dumps(preview_view(preview), ensure_ascii=False)
        applied = apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_T1
        )
        card_body = json.dumps(card_view(applied), ensure_ascii=False)
    for body in (preview_body, card_body):
        lowered = body.lower()
        for forbidden in ("storage_key", "file_id", "pdf"):
            assert forbidden not in lowered, f"重写响应不得包含来源字段: {forbidden}"
