"""今日学习计划（structure-contract 3.20/6.6；openapi /study/today，V25-D-39 账号级）。

服务级用例（固定 now 注入，验证确定性语义）：
- 主计划 = 账号计划所选卡组内全部已学习（state != NEW）且到期（due <= now）的可见卡，
  按遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序取到每日目标；
- 仍有余额时从同一批卡组的 NEW 卡按 deck_id、position、card_id 补足；
- 计划卡组可跨项目与独立卡组（V25-D-39）：今日队列跨卡组聚合；
- 学习日期/今日去重完成数按账号 IANA 学习时区分桶（UTC reviewed_at 折算，不改写事件）；
- 统一可见谓词：STAGED 与删除批次中的卡不进计划、不计到期总数；
- 评级幂等：同 (学习日期, card_id) 只计一次今日完成；
- 未保存计划（无目标行或零卡组）返回诚实空态，不隐式收录任何卡组。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session

from app.schemas.review import ReviewState as ReviewStateView
from app.schemas.study_plan import TodayPlanCard, TodayStudyPlan
from infra.db.models import (
    Base,
    Card,
    CardDeletionBatch,
    Chapter,
    Deck,
    LearningProject,
    Material,
    PdfFile,
    ReviewEvent,
    ReviewState,
    User,
    UserPreferences,
    UserStudyDeck,
    UserStudySettings,
)
from infra.db.session import create_db_engine, create_session_factory, format_utc
from services.review.service import submit_review
from services.study.service import today_study_plan

_NOW = "2026-08-15T12:00:00.000Z"  # 固定服务端时钟（UTC）
_NOW_DT = datetime(2026, 8, 15, 12, 0, 0, tzinfo=UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


def _fmt(dt: datetime) -> str:
    return format_utc(dt)


def _plan(session: Session, *, user_id: str, now: str) -> TodayStudyPlan:
    """今日计划 dict → 契约 schema（typed 断言锚点）。"""
    return TodayStudyPlan.model_validate(today_study_plan(session, user_id=user_id, now=now))


def _card_ids(plan: TodayStudyPlan) -> list[str]:
    return [c.card_id for c in plan.cards]


def _state(card: TodayPlanCard) -> ReviewStateView:
    """TodayPlanCard 内嵌排程状态（契约全量必回；断言防 None）。"""
    assert card.review_state is not None
    return card.review_state


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'study.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


@pytest.fixture
def user() -> str:
    return _uuid()


def _seed_user(session: Session, user_id: str) -> None:
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


def _seed_project(
    session: Session, *, user_id: str, chapters: int = 2, deck_count: int = 1
) -> dict[str, object]:
    """PARSED PDF + 章节 + 学习项目 + 项目牌组（deck.project_id 绑定；ORM 直种）。"""
    file_id = _uuid()
    session.add(
        PdfFile(
            file_id=file_id,
            user_id=user_id,
            filename="seed.pdf",
            storage_key="a" * 32,
            size_bytes=100,
            status="PARSED",
            created_at=_NOW,
        )
    )
    session.flush()
    project_id = _uuid()
    session.add(
        Material(
            material_id=file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project_id,
            type="PDF",
            name="seed.pdf",
            status=None,
            size_bytes=100,
            created_at=_NOW,
        )
    )
    session.add(
        LearningProject(
            project_id=project_id,
            user_id=user_id,
            name="种子项目",
            chapters_confirmed_at=_NOW,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()
    chapter_ids = [str(uuid.uuid4()) for _ in range(chapters)]
    for i, cid in enumerate(chapter_ids):
        session.add(
            Chapter(
                chapter_id=cid,
                file_id=file_id,
                material_id=file_id,
                name=f"第{i + 1}章",
                start_page=i * 10 + 1,
                end_page=i * 10 + 10,
            )
        )
    deck_ids = [str(uuid.uuid4()) for _ in range(deck_count)]
    for did in deck_ids:
        session.add(
            Deck(
                deck_id=did,
                user_id=user_id,
                name="D",
                source="MANUAL",
                project_id=project_id,
                version=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
    session.flush()
    return {"project_id": project_id, "chapter_ids": chapter_ids, "deck_ids": deck_ids}


def _seed_card(
    session: Session,
    *,
    user_id: str,
    deck_id: str,
    position: int,
    state: str = "NEW",
    stability: float = 0.0,
    difficulty: float = 1.0,
    due: str = _NOW,
    last_review: str | None = None,
    reps: int = 0,
    lapses: int = 0,
    last_rating: str | None = None,
    chapter_id: str | None = None,
    target_difficulty: str | None = None,
    publication_state: str = "PUBLISHED",
    delete_batch_id: str | None = None,
    card_id: str | None = None,
    front: str = "f",
) -> str:
    cid = card_id or _uuid()
    if delete_batch_id is not None:
        session.add(
            CardDeletionBatch(  # FK：删除批次行须存在（Task 8 语义：批次即删除证据）
                delete_batch_id=delete_batch_id,
                user_id=user_id,
                status="PENDING",
                undo_until=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()  # unitofwork 不保证批次先于卡插入——显式落批次行（Task 8 同款）
    session.add(
        Card(
            card_id=cid,
            deck_id=deck_id,
            user_id=user_id,
            source="MANUAL",
            position=position,
            front=front,
            back="b",
            card_type="QUESTION",
            chapter_id=chapter_id,
            target_difficulty=target_difficulty,
            publication_state=publication_state,
            delete_batch_id=delete_batch_id,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.add(
        ReviewState(
            review_state_id=_uuid(),
            card_id=cid,
            state=state,
            stability=stability,
            difficulty=difficulty,
            due=due,
            last_review=last_review,
            reps=reps,
            lapses=lapses,
            last_rating=last_rating,
            updated_at=_NOW,
        )
    )
    return cid


def _seed_preferences(
    session: Session,
    *,
    user_id: str,
    daily_goal: int = 50,
    timezone: str = "Asia/Shanghai",
) -> None:
    session.add(
        UserPreferences(
            user_id=user_id,
            coverage_mode="BALANCED",
            basic_ratio=40,
            understanding_ratio=40,
            deep_question_ratio=20,
            daily_goal=daily_goal,
            learning_timezone=timezone,
            current_project_id=None,
            updated_at=_NOW,
        )
    )
    session.flush()


def _seed_user_plan(
    session: Session,
    *,
    user_id: str,
    deck_ids: list[str],
    daily_new_goal: int = 0,
    daily_review_goal: int = 10,
) -> None:
    """账号级计划（V25-D-39）：一用户一行目标 + 计划卡组集合（可跨项目与独立卡组）。"""
    session.add(
        UserStudySettings(
            user_id=user_id,
            daily_new_goal=daily_new_goal,
            daily_review_goal=daily_review_goal,
            updated_at=_NOW,
        )
    )
    for did in deck_ids:
        session.add(UserStudyDeck(user_id=user_id, deck_id=did, created_at=_NOW))
    session.flush()


def _seed_event(
    session: Session, *, user_id: str, card_id: str, client_event_id: str, reviewed_at: str
) -> None:
    session.add(
        ReviewEvent(
            review_event_id=_uuid(),
            user_id=user_id,
            card_id=card_id,
            client_event_id=client_event_id,
            rating="GOOD",
            reviewed_at=reviewed_at,
            device_timezone=None,
            created_at=reviewed_at,
        )
    )
    session.flush()


# ---------- 到期优先与稳定排序 ----------


def test_today_plan_due_first_ordering_by_risk_overdue_card_id(
    session_factory: Callable[[], Session], user: str
) -> None:
    """遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序（3.20）。

    风险 = 1 - FSRS 可检索性 R(now)（scheduler.forgetting_risk）：
    A: S=2 距今 10d → 风险 0.322；B: S=30 距今 30d → 0.100；
    E: S=10 距今 10d 但仅逾期 2d → 0.100（与 B 并列，逾期短 → B 先）；
    D/F: S=15/S=10 逾期 10d → 0.070/0.100；C: S=100 → 0.033。
    期望全序：A → B → F(0.100 逾期 10d) → G(0.100 逾期 10d, card_id 升序) → E → D → C。
    """
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ago10 = _fmt(_NOW_DT - timedelta(days=10))
        ago30 = _fmt(_NOW_DT - timedelta(days=30))
        ago2 = _fmt(_NOW_DT - timedelta(days=2))
        cards = {
            "a": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=1,
                state="REVIEW",
                stability=2.0,
                due=ago10,
                last_review=ago10,
                reps=5,
                card_id="00000000-0000-0000-0000-000000000001",
            ),
            "b": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=2,
                state="REVIEW",
                stability=30.0,
                due=ago30,
                last_review=ago30,
                reps=9,
                card_id="00000000-0000-0000-0000-000000000002",
            ),
            "c": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=3,
                state="REVIEW",
                stability=100.0,
                due=ago30,
                last_review=ago30,
                reps=3,
                card_id="00000000-0000-0000-0000-000000000003",
            ),
            "d": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=4,
                state="REVIEW",
                stability=15.0,
                due=ago10,
                last_review=ago10,
                reps=2,
                card_id="00000000-0000-0000-0000-000000000004",
            ),
            "e": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=5,
                state="REVIEW",
                stability=10.0,
                due=ago2,
                last_review=ago10,
                reps=4,
                card_id="00000000-0000-0000-0000-000000000005",
            ),
            "f": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=6,
                state="REVIEW",
                stability=10.0,
                due=ago10,
                last_review=ago10,
                reps=1,
                card_id="00000000-0000-0000-0000-000000000006",
            ),
            "g": _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=7,
                state="REVIEW",
                stability=10.0,
                due=ago10,
                last_review=ago10,
                reps=1,
                card_id="00000000-0000-0000-0000-000000000007",
            ),
        }
        _seed_preferences(session, user_id=user, daily_goal=50)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=0, daily_review_goal=40
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert [c.card_id for c in plan.cards] == [
        cards["a"],
        cards["b"],
        cards["f"],
        cards["g"],
        cards["e"],
        cards["d"],
        cards["c"],
    ]
    assert plan.due_count == 7
    assert plan.backlog_count == 0
    assert plan.main_plan_remaining == 7
    # 风险字段：可计算卡 > 0，新卡填充风险 0（此处无新卡）
    risks: dict[str, float] = {c.card_id: cast(float, c.forgetting_risk) for c in plan.cards}
    assert risks[cards["a"]] > risks[cards["b"]] > risks[cards["c"]]
    assert risks[cards["f"]] == pytest.approx(risks[cards["g"]])


# ---------- 账号级跨项目聚合（V25-D-39） ----------


def test_today_plan_aggregates_decks_across_projects(
    session_factory: Callable[[], Session], user: str
) -> None:
    """计划卡组跨项目混选：今日队列按遗忘风险全局排序聚合，不按项目分桶。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx_a = _seed_project(session, user_id=user, chapters=1)
        ctx_b = _seed_project(session, user_id=user, chapters=1)
        deck_a = cast(list[str], ctx_a["deck_ids"])[0]
        deck_b = cast(list[str], ctx_b["deck_ids"])[0]
        ago = _fmt(_NOW_DT - timedelta(days=10))
        # 项目 A 的卡风险高（S=2），项目 B 的卡风险低（S=100）：A 先出
        risky = _seed_card(
            session,
            user_id=user,
            deck_id=deck_a,
            position=1,
            state="REVIEW",
            stability=2.0,
            due=ago,
            last_review=ago,
            reps=5,
        )
        stable = _seed_card(
            session,
            user_id=user,
            deck_id=deck_b,
            position=1,
            state="REVIEW",
            stability=100.0,
            due=ago,
            last_review=ago,
            reps=3,
        )
        _seed_preferences(session, user_id=user, daily_goal=50)
        _seed_user_plan(
            session,
            user_id=user,
            deck_ids=[deck_a, deck_b],
            daily_new_goal=0,
            daily_review_goal=10,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.plan_configured is True
    assert plan.due_count == 2
    assert _card_ids(plan) == [risky, stable]
    assert set(plan.selected_deck_ids) == {deck_a, deck_b}


def test_today_plan_unselected_decks_stay_out(
    session_factory: Callable[[], Session], user: str
) -> None:
    """计划外卡组（同项目另一卡组）不进入今日队列——范围约束不因项目归属放宽。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=1, deck_count=2)
        deck_in, deck_out = cast(list[str], ctx["deck_ids"])
        ago = _fmt(_NOW_DT - timedelta(days=5))
        in_card = _seed_card(
            session,
            user_id=user,
            deck_id=deck_in,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        _seed_card(
            session,
            user_id=user,
            deck_id=deck_out,
            position=1,
            state="REVIEW",
            stability=2.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        _seed_preferences(session, user_id=user, daily_goal=50)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck_in], daily_new_goal=0, daily_review_goal=10
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.due_count == 1
    assert _card_ids(plan) == [in_card]


def test_today_plan_standalone_deck_joins_account_plan(
    session_factory: Callable[[], Session], user: str
) -> None:
    """独立牌组（project_id=null）被选入账号计划后进入今日队列（V25-D-39 语义翻转）。"""
    from services.review.service import review_queue

    with session_factory() as session:
        _seed_user(session, user)
        _seed_project(session, user_id=user, chapters=1)
        ago = _fmt(_NOW_DT - timedelta(days=5))
        independent_deck = _uuid()
        session.add(
            Deck(
                deck_id=independent_deck,
                user_id=user,
                name="独立",
                source="MANUAL",
                project_id=None,
                version=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()  # 独立牌组行先落库（unitofwork 不保证卡先于牌组插入——Task 8 同款）
        due_card = _seed_card(
            session,
            user_id=user,
            deck_id=independent_deck,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(
            session,
            user_id=user,
            deck_ids=[independent_deck],
            daily_new_goal=0,
            daily_review_goal=10,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert _card_ids(plan) == [due_card]
    # 独立牌组到期复习入口不受影响（6.6：独立牌组可启动自己的到期复习）
    with session_factory() as session:
        items = review_queue(session, user_id=user, deck_id=independent_deck, now=_NOW)
    assert len(items) == 1


# ---------- 超目标逾期积压 ----------


def test_today_plan_backlog_beyond_daily_goal(
    session_factory: Callable[[], Session], user: str
) -> None:
    """到期数 >= 巩固目标：主计划只安排到期卡（不加入新卡），积压 = 到期总数 - 目标。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ago = _fmt(_NOW_DT - timedelta(days=5))
        for i in range(12):
            _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=i + 1,
                state="REVIEW",
                stability=10.0,
                due=ago,
                last_review=ago,
                reps=1,
            )
        # 计划内新卡：巩固目标已满，不得进入计划
        _seed_card(session, user_id=user, deck_id=deck, position=20, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=0, daily_review_goal=10
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.daily_goal == 10  # 实际核心队列目标 = 巩固 10 + 新学 0
    assert plan.due_count == 12
    assert plan.backlog_count == 2  # 到期总数超出每日目标的部分
    assert len(plan.cards) == 10  # 主计划只安排到期卡
    assert plan.main_plan_remaining == 10
    assert all(_state(c).state != "NEW" for c in plan.cards)


def test_today_plan_due_less_than_goal_no_backlog(
    session_factory: Callable[[], Session], user: str
) -> None:
    """到期数 < 巩固目标：全部到期卡进计划，积压为 0。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ago = _fmt(_NOW_DT - timedelta(days=5))
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=2,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=0, daily_review_goal=10
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.due_count == 2
    assert plan.backlog_count == 0
    assert len(plan.cards) == 2


# ---------- 新卡补足（卡组顺序） ----------


def test_today_plan_new_card_fill_by_deck_position_order(
    session_factory: Callable[[], Session], user: str
) -> None:
    """余额由计划卡组 NEW 卡按 deck_id、position、card_id 稳定补足（3.20/FR-02）。"""
    deck_a = "00000000-0000-0000-0000-00000000a001"
    deck_b = "00000000-0000-0000-0000-00000000b001"
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=1)
        project_id = cast(str, ctx["project_id"])
        for did in (deck_a, deck_b):
            session.add(
                Deck(
                    deck_id=did,
                    user_id=user,
                    name=f"D-{did[-1]}",
                    source="MANUAL",
                    project_id=project_id,
                    version=_NOW,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
        session.flush()
        ago = _fmt(_NOW_DT - timedelta(days=3))
        due_card = _seed_card(
            session,
            user_id=user,
            deck_id=deck_a,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=2,
        )
        # 新卡：deck_a 两张（position 2/3）、deck_b 两张（position 1/2）——全局按 (deck_id, position) 排序
        new_a1 = _seed_card(session, user_id=user, deck_id=deck_a, position=2, state="NEW")
        new_a2 = _seed_card(session, user_id=user, deck_id=deck_a, position=3, state="NEW")
        new_b1 = _seed_card(session, user_id=user, deck_id=deck_b, position=1, state="NEW")
        new_b2 = _seed_card(session, user_id=user, deck_id=deck_b, position=2, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=20)
        _seed_user_plan(
            session,
            user_id=user,
            deck_ids=[deck_b, deck_a],
            daily_new_goal=10,
            daily_review_goal=10,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    # 新学补足跨卡组全局排序：deck_a(p2) → deck_a(p3) → deck_b(p1) → deck_b(p2)
    assert _card_ids(plan) == [due_card, new_a1, new_a2, new_b1, new_b2]
    assert plan.due_count == 1
    assert plan.main_plan_remaining == 5
    assert plan.backlog_count == 0


# ---------- IANA 时区每日重置与今日去重完成数 ----------


def test_today_plan_study_date_and_completed_reset_by_iana_timezone(
    session_factory: Callable[[], Session], user: str
) -> None:
    """学习日期与今日去重完成数按账号 IANA 学习时区分桶（1.2/3.20，UTC reviewed_at 折算）。

    America/Los_Angeles（8 月 UTC-7）：
    - 2026-08-15T23:30Z / 2026-08-16T01:00Z 均落在 LA 的 08-15 → 同日完成去重为 1 张；
    - 2026-08-16T08:00Z 落在 LA 的 08-16 → 新学习日计数重置。
    """
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ago = _fmt(_NOW_DT - timedelta(days=5))
        card_a = _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=2,
        )
        card_b = _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=2,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=2,
        )
        # 同一张卡同日两次评级（23:30Z 与次日 01:00Z 都属 LA 08-15）：只计一次完成
        _seed_event(
            session,
            user_id=user,
            card_id=card_a,
            client_event_id=_uuid(),
            reviewed_at="2026-08-15T23:30:00.000Z",
        )
        _seed_event(
            session,
            user_id=user,
            card_id=card_a,
            client_event_id=_uuid(),
            reviewed_at="2026-08-16T01:00:00.000Z",
        )
        _seed_event(
            session,
            user_id=user,
            card_id=card_b,
            client_event_id=_uuid(),
            reviewed_at="2026-08-16T01:00:00.000Z",
        )
        _seed_preferences(session, user_id=user, daily_goal=10, timezone="America/Los_Angeles")
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=0, daily_review_goal=10
        )
        session.commit()

    # LA 08-15 白天：23:30Z 与 01:00Z 的评级都算今天（LA 时间 07-15 16:30 / 18:00）
    with session_factory() as session:
        plan = _plan(session, user_id=user, now="2026-08-15T23:30:00.000Z")
    assert plan.timezone == "America/Los_Angeles"
    assert plan.study_date == "2026-08-15"
    assert plan.today_completed_count == 2  # (LA 08-15, card_a) 与 (LA 08-15, card_b)

    # LA 午夜之后：新学习日 08-16，只算 08:00Z 的评级
    with session_factory() as session:
        plan = _plan(session, user_id=user, now="2026-08-16T08:00:00.000Z")
    assert plan.study_date == "2026-08-16"
    assert plan.today_completed_count == 0  # 08:00Z 的评级尚未发生（未来）→ 重置语义

    # 补上 08-16 的事件后：今日完成 = 1（同一张卡跨学习日可各计一次）
    with session_factory() as session:
        _seed_event(
            session,
            user_id=user,
            card_id=card_a,
            client_event_id=_uuid(),
            reviewed_at="2026-08-16T08:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        plan = _plan(session, user_id=user, now="2026-08-16T08:00:00.000Z")
    assert plan.today_completed_count == 1


def test_today_plan_duplicate_rating_idempotency_single_completion(
    session_factory: Callable[[], Session], user: str
) -> None:
    """重复评分幂等：同 client_event_id 重放不新增事件；同日同卡多次评级只计一次今日完成。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        card_a = _seed_card(session, user_id=user, deck_id=deck, position=1, state="NEW")
        card_b = _seed_card(session, user_id=user, deck_id=deck, position=2, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=10, daily_review_goal=10
        )
        session.commit()

    client_event = _uuid()
    with session_factory() as session:
        result = submit_review(
            session,
            user_id=user,
            card_id=card_a,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone=None,
            now=_NOW,
        )
        session.commit()
        assert (
            cast(str, result["study_date"]) == "2026-08-15"
        )  # Asia/Shanghai 默认时区（UTC+8 同日）
        assert cast(dict[str, object], result["review_state"])["state"] == "LEARNING"
    # 同 client_event_id 重放：事件不重复
    with session_factory() as session:
        submit_review(
            session,
            user_id=user,
            card_id=card_a,
            rating="GOOD",
            client_event_id=client_event,
            device_timezone=None,
            now=_NOW,
        )
        session.commit()
    # 同日同卡再评级（不同事件）：完成数仍为 1（同一 (学习日期, card_id) 只计一次）
    with session_factory() as session:
        submit_review(
            session,
            user_id=user,
            card_id=card_a,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone=None,
            now=_NOW,
        )
        session.commit()
        submit_review(
            session,
            user_id=user,
            card_id=card_b,
            rating="GOOD",
            client_event_id=_uuid(),
            device_timezone=None,
            now=_NOW,
        )
        session.commit()
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.today_completed_count == 2  # card_a(去重后 1) + card_b(1)


# ---------- 可见谓词：已删 / STAGED 排除 ----------


def test_today_plan_excludes_deleted_and_staged_cards(
    session_factory: Callable[[], Session], user: str
) -> None:
    """STAGED 与删除批次中的卡不进到期队列/新卡补足，也不计入到期总数（统一可见谓词 3.9）。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ago = _fmt(_NOW_DT - timedelta(days=5))
        visible = _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=1,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
        )
        # 已评级但进入删除批次 / STAGED 未发布的到期卡：均不可见
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=2,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
            delete_batch_id=_uuid(),
        )
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=3,
            state="REVIEW",
            stability=10.0,
            due=ago,
            last_review=ago,
            reps=1,
            publication_state="STAGED",
        )
        # 计划内新卡：删除批次 / STAGED 的同样不可见
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=4,
            state="NEW",
            delete_batch_id=_uuid(),
        )
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=5,
            state="NEW",
            publication_state="STAGED",
        )
        visible_new = _seed_card(session, user_id=user, deck_id=deck, position=6, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(
            session, user_id=user, deck_ids=[deck], daily_new_goal=10, daily_review_goal=10
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.due_count == 1  # 只有可见的已学习到期卡
    assert _card_ids(plan) == [visible, visible_new]


# ---------- 空态（V25-D-39：未保存计划） ----------


def test_today_plan_empty_state_without_saved_plan(
    session_factory: Callable[[], Session], user: str
) -> None:
    """无账号级计划行：plan_configured=false 空态，计划为空；今日完成仍按账号全天去重。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        card = _seed_card(session, user_id=user, deck_id=deck, position=1, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_event(
            session,
            user_id=user,
            card_id=card,
            client_event_id=_uuid(),
            reviewed_at=_NOW,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.plan_configured is False
    assert plan.cards == []
    assert plan.due_count == 0
    assert plan.main_plan_remaining == 0
    assert plan.backlog_count == 0
    assert plan.today_completed_count == 1  # 账号全天去重完成仍如实计数


def test_today_plan_settings_row_without_decks_is_empty_state(
    session_factory: Callable[[], Session], user: str
) -> None:
    """有目标行但零卡组（不一致防御）：同样返回诚实空态，不隐式收录任何卡组。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        _seed_card(session, user_id=user, deck_id=deck, position=1, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10)
        _seed_user_plan(session, user_id=user, deck_ids=[], daily_new_goal=0, daily_review_goal=10)
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.plan_configured is False
    assert plan.cards == []
