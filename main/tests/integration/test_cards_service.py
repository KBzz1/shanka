"""services.cards 集成测试：position/创建/列表/导入原子/初始 review_state/进度派生（user 域）。"""

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AppError, ErrorCode
from infra.db.models import Base, ReviewState, User
from infra.db.session import create_db_engine, create_session_factory
from services.cards.service import create_card, import_cards, list_cards
from services.decks.service import create_deck, deck_progress


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'cards.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _ensure_user(session: Session, user_id: str) -> None:
    """users 行先落库：HTTP 流由注册端点建立，service 测试需显式建立（FK 强制）。"""
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            password_hash="x",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
    )
    session.flush()


@pytest.fixture
def deck_id(session_factory: Callable[[], Session]) -> str:
    """牌组归属 dev 用户（_owned 语义），users 行前置（carry-forward：FK 违约已实证）。"""
    with session_factory() as session:
        _ensure_user(session, "dev")
        deck = create_deck(session, user_id="dev", name="D", now="2026-08-11T00:00:00.000Z")
        session.commit()
        return deck.deck_id


def test_cards_create_assigns_incrementing_position_and_initial_state(
    session_factory: Callable[[], Session], deck_id: str
) -> None:
    with session_factory() as session:
        c1 = create_card(
            session,
            user_id="dev",
            deck_id=deck_id,
            front="f1",
            back="b1",
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
        c1_id = c1.card_id
    with session_factory() as session:
        c2 = create_card(
            session,
            user_id="dev",
            deck_id=deck_id,
            front="f2",
            back="b2",
            now="2026-08-11T00:00:00.001Z",
        )
        session.commit()
    assert c1.position == 1
    assert c2.position == 2  # 追加不覆盖
    with session_factory() as session:
        cards = list_cards(session, user_id="dev", deck_id=deck_id)
        assert [c.position for c in cards] == [1, 2]  # 稳定排序
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == c1_id))
        assert rs is not None
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0  # ORM CHECK 1~10（Task 2 已踩坑：0.0 违约）
        assert rs.due == "2026-08-11T00:00:00.000Z"
        # carry-forward：创建卡后进度非零且正确（初始 due=now 恒到期；now 取最后一张卡 due）
        progress = deck_progress(
            session, user_id="dev", deck_id=deck_id, now="2026-08-11T00:00:00.001Z"
        )
        assert progress["card_count"] == 2
        assert progress["due_count"] == 2
        assert progress["mastered_card_count"] == 0
        assert progress["review_count"] == 0


def test_cards_create_other_user_404(session_factory: Callable[[], Session], deck_id: str) -> None:
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_card(
            session,
            user_id="other",
            deck_id=deck_id,
            front="f",
            back="b",
            now="2026-08-11T00:00:00.000Z",
        )
    assert excinfo.value.code is ErrorCode.DECK_NOT_FOUND


def test_cards_import_atomic_all_or_nothing(
    session_factory: Callable[[], Session], deck_id: str
) -> None:
    """原子导入：成功全部入库；中途异常整体回滚（无部分写入）。"""
    with session_factory() as session:
        results = import_cards(
            session,
            user_id="dev",
            deck_id=deck_id,
            cards=[("f1", "b1"), ("f2", "b2")],
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    assert [r["index"] for r in results] == [0, 1]
    assert all(r["status"] == "CREATED" for r in results)
    assert all(r["card_id"] for r in results)
    with session_factory() as session:
        cards = list_cards(session, user_id="dev", deck_id=deck_id)
        assert len(cards) == 2
        assert [c.position for c in cards] == [1, 2]


def test_cards_import_rolls_back_on_write_failure(
    session_factory: Callable[[], Session], deck_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入中途写入失败：整体回滚（原子性），已插入卡片不残留。"""
    import services.cards.service as cards_service

    with session_factory() as session:
        # 让第二张卡的 card_id 与第一张相同 → cards PK 冲突（IntegrityError）
        real_uuid = uuid.uuid4
        n = 0
        first_card_id = ""

        def fake_uuid() -> str:
            nonlocal n, first_card_id
            n += 1
            if n == 1:
                first_card_id = str(real_uuid())
                return first_card_id
            if n == 3:  # 第二张卡 card_id 复用第一张
                return first_card_id
            return str(real_uuid())

        monkeypatch.setattr(cards_service, "_card_id", fake_uuid)
        with pytest.raises(IntegrityError):
            import_cards(
                session,
                user_id="dev",
                deck_id=deck_id,
                cards=[("f1", "b1"), ("f2", "b2")],
                now="2026-08-11T00:00:00.000Z",
            )
        session.rollback()
    with session_factory() as session:
        cards = list_cards(session, user_id="dev", deck_id=deck_id)
        assert len(cards) == 0  # 原子：无部分写入


def test_cards_import_position_continues_after_existing(
    session_factory: Callable[[], Session], deck_id: str
) -> None:
    with session_factory() as session:
        create_card(
            session,
            user_id="dev",
            deck_id=deck_id,
            front="f0",
            back="b0",
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        import_cards(
            session,
            user_id="dev",
            deck_id=deck_id,
            cards=[("f1", "b1")],
            now="2026-08-11T00:00:00.001Z",
        )
        session.commit()
    with session_factory() as session:
        cards = list_cards(session, user_id="dev", deck_id=deck_id)
        assert [c.position for c in cards] == [1, 2]
