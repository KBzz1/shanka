"""今日学习计划（structure-contract 3.20/6.6；openapi /study/today，V2.5 新增）。

服务级用例（固定 now 注入，验证确定性语义）：
- 主计划 = 当前项目全部已学习（state != NEW）且到期（due <= now）的可见卡，
  按遗忘风险 DESC → 逾期时长 DESC → card_id 稳定排序取到每日目标；
- 仍有余额时从项目学习范围（selected_new_card_chapter_ids + include_unassigned）中的
  NEW 卡按选定章节顺序、position、card_id 补足；
- 学习日期/今日去重完成数按账号 IANA 学习时区分桶（UTC reviewed_at 折算，不改写事件）；
- 统一可见谓词：STAGED 与删除批次中的卡不进计划、不计到期总数；
- 评级幂等：同 (学习日期, card_id) 只计一次今日完成。

决策记录（实现裁决，报告同步）：
- main_plan_remaining = 本次返回 cards 列表长度——已评级卡 due 自动推到未来、
  NEW 卡评级后离开 NEW 集，重取计划时自然不再出现，故队列长度即剩余主计划数；
  "继续复习"积压卡（due_count > 每日目标）由下次 GET 的到期优先队列继续供给。
- due_count = 已学习（state != NEW）且到期的可见卡数（NEW 卡不是"待复习"）。
- 新卡章节顺序 = 学习设置保存的选定章节数组顺序（选定章节顺序，FR-09），
  未归属分组（include_unassigned=true）排在所有选定章节之后。
"""

import json
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
    ProjectStudySettings,
    ReviewEvent,
    ReviewState,
    User,
    UserPreferences,
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
    current_project_id: str | None = None,
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
            current_project_id=current_project_id,
            updated_at=_NOW,
        )
    )
    session.flush()


def _seed_study_settings(
    session: Session, *, project_id: str, selected: list[str], include_unassigned: bool = False
) -> None:
    session.add(
        ProjectStudySettings(
            project_id=project_id,
            selected_chapter_ids=json.dumps(selected),
            include_unassigned=1 if include_unassigned else 0,
            updated_at=_NOW,
        )
    )
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
        _seed_preferences(
            session, user_id=user, daily_goal=50, current_project_id=cast(str, ctx["project_id"])
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


# ---------- 超目标逾期积压 ----------


def test_today_plan_backlog_beyond_daily_goal(
    session_factory: Callable[[], Session], user: str
) -> None:
    """到期数 >= 每日目标：主计划只安排到期卡（不加入新卡），积压 = 到期总数 - 目标。"""
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
        # 范围内新卡：目标已满，不得进入计划
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=20,
            state="NEW",
            chapter_id=cast(list[str], ctx["chapter_ids"])[0],
        )
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        _seed_study_settings(
            session,
            project_id=cast(str, ctx["project_id"]),
            selected=[cast(list[str], ctx["chapter_ids"])[0]],
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.daily_goal == 10
    assert plan.due_count == 12
    assert plan.backlog_count == 2  # 到期总数超出每日目标的部分
    assert len(plan.cards) == 10  # 主计划只安排到期卡
    assert plan.main_plan_remaining == 10
    assert all(_state(c).state != "NEW" for c in plan.cards)


def test_today_plan_due_less_than_goal_no_backlog(
    session_factory: Callable[[], Session], user: str
) -> None:
    """到期数 < 每日目标：全部到期卡进计划，积压为 0。"""
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
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.due_count == 2
    assert plan.backlog_count == 0
    assert len(plan.cards) == 2


# ---------- 新卡补足与章节范围 ----------


def test_today_plan_new_card_fill_by_selected_chapter_order(
    session_factory: Callable[[], Session], user: str
) -> None:
    """余额由范围内 NEW 卡按选定章节顺序、position、card_id 补足（3.20/FR-02/FR-09）。

    settings 选定顺序 [ch2, ch1]（客户端选定序）：ch2 新卡先于 ch1；未选定章节与
    未归属（include_unassigned=false）的新卡不进入计划。
    """
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=3)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ch1, ch2, ch3 = cast(list[str], ctx["chapter_ids"])
        ago = _fmt(_NOW_DT - timedelta(days=3))
        due_cards = [
            _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=1,
                state="REVIEW",
                stability=10.0,
                due=ago,
                last_review=ago,
                reps=2,
                card_id="00000000-0000-0000-0000-000000000011",
            ),
            _seed_card(
                session,
                user_id=user,
                deck_id=deck,
                position=2,
                state="REVIEW",
                stability=10.0,
                due=ago,
                last_review=ago,
                reps=2,
                card_id="00000000-0000-0000-0000-000000000012",
            ),
        ]
        # 新卡：ch1 两张（position 3/4）、ch2 一张（position 5）、ch3 一张（范围外）、未归属一张
        new_ch1_1 = _seed_card(
            session, user_id=user, deck_id=deck, position=3, state="NEW", chapter_id=ch1
        )
        new_ch1_2 = _seed_card(
            session, user_id=user, deck_id=deck, position=4, state="NEW", chapter_id=ch1
        )
        new_ch2 = _seed_card(
            session, user_id=user, deck_id=deck, position=5, state="NEW", chapter_id=ch2
        )
        _seed_card(session, user_id=user, deck_id=deck, position=6, state="NEW", chapter_id=ch3)
        _seed_card(session, user_id=user, deck_id=deck, position=7, state="NEW")  # 未归属
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        _seed_study_settings(
            session,
            project_id=cast(str, ctx["project_id"]),
            selected=[ch2, ch1],
            include_unassigned=False,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert [c.card_id for c in plan.cards] == [
        *due_cards,
        new_ch2,
        new_ch1_1,
        new_ch1_2,  # ch2 先于 ch1（选定顺序）；ch3/未归属排除
    ]
    assert plan.due_count == 2
    assert plan.main_plan_remaining == 5
    assert plan.backlog_count == 0


def test_today_plan_unassigned_new_cards_only_with_include_unassigned(
    session_factory: Callable[[], Session], user: str
) -> None:
    """未归属新卡（chapter_id=null）：include_unassigned=false 排除；true 时排在选定章节之后。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        ch1 = cast(list[str], ctx["chapter_ids"])[0]
        new_ch1 = _seed_card(
            session, user_id=user, deck_id=deck, position=1, state="NEW", chapter_id=ch1
        )
        unassigned = _seed_card(session, user_id=user, deck_id=deck, position=2, state="NEW")
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        _seed_study_settings(
            session,
            project_id=cast(str, ctx["project_id"]),
            selected=[ch1],
            include_unassigned=False,
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert [c.card_id for c in plan.cards] == [new_ch1]

    with session_factory() as session:
        settings = session.get(ProjectStudySettings, cast(str, ctx["project_id"]))
        assert settings is not None
        settings.include_unassigned = 1
        settings.updated_at = _NOW
        session.commit()
    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert [c.card_id for c in plan.cards] == [new_ch1, unassigned]


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
        _seed_preferences(
            session,
            user_id=user,
            daily_goal=10,
            timezone="America/Los_Angeles",
            current_project_id=cast(str, ctx["project_id"]),
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
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
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
        ch1 = cast(list[str], ctx["chapter_ids"])[0]
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
        # 范围内新卡：删除批次 / STAGED 的同样不可见
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=4,
            state="NEW",
            chapter_id=ch1,
            delete_batch_id=_uuid(),
        )
        _seed_card(
            session,
            user_id=user,
            deck_id=deck,
            position=5,
            state="NEW",
            chapter_id=ch1,
            publication_state="STAGED",
        )
        visible_new = _seed_card(
            session, user_id=user, deck_id=deck, position=6, state="NEW", chapter_id=ch1
        )
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        _seed_study_settings(session, project_id=cast(str, ctx["project_id"]), selected=[ch1])
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.due_count == 1  # 只有可见的已学习到期卡
    assert [c.card_id for c in plan.cards] == [visible, visible_new]


# ---------- 空态与独立牌组 ----------


def test_today_plan_empty_state_without_current_project(
    session_factory: Callable[[], Session], user: str
) -> None:
    """无当前项目：current_project=null 空态，计划为空；今日完成仍按账号全天去重。"""
    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
        deck = cast(list[str], ctx["deck_ids"])[0]
        _seed_card(session, user_id=user, deck_id=deck, position=1, state="NEW")
        _seed_preferences(session, user_id=user, daily_goal=10, current_project_id=None)
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.current_project is None
    assert plan.cards == []
    assert plan.due_count == 0
    assert plan.main_plan_remaining == 0
    assert plan.backlog_count == 0
    assert plan.today_completed_count == 0


def test_today_plan_independent_deck_excluded_from_project_plan(
    session_factory: Callable[[], Session], user: str
) -> None:
    """独立牌组（project_id=null）不进入首页每日计划（V25-STUDY-FR-01）；到期卡仍可独立复习。"""
    from services.review.service import review_queue

    with session_factory() as session:
        _seed_user(session, user)
        ctx = _seed_project(session, user_id=user, chapters=2)
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
        _seed_card(
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
        _seed_preferences(
            session, user_id=user, daily_goal=10, current_project_id=cast(str, ctx["project_id"])
        )
        session.commit()

    with session_factory() as session:
        plan = _plan(session, user_id=user, now=_NOW)
    assert plan.cards == []  # 项目内无卡 → 计划为空
    # 独立牌组到期复习不受影响（6.6：独立牌组可启动自己的到期复习）
    with session_factory() as session:
        items = review_queue(session, user_id=user, deck_id=independent_deck, now=_NOW)
    assert len(items) == 1
