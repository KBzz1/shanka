"""V2.5 真实时区安全统计集成测试（Task 11；structure-contract 3.12 重写口径）。

服务端按账号学习时区（preferences.learning_timezone）分桶：周活动/周完成去重/streak/
周界全部折算到账号 IANA 时区；周目标 = daily_learning_goal × 7，不再接受客户端
timezone/weekly_goal 参数（未知查询参数 → 400 VALIDATION_ERROR）。
统计源 = review_events（评级事件）：自由刷题不写事件不计统计；STAGED 与删除批次卡
（统一可见谓词 domain/card.py:12）从全部统计口径排除（不保留幽灵计数）。

学习日期换算复用 services.preferences.service.learning_date（UTC reviewed_at → 账号
时区 ISO 日期，不改写事件；时区改变后用新时区重新分桶）——本测试断言即该复用语义。

DST 事实（zoneinfo 实测）：
- America/New_York 2026-03-08 02:00 EST → 03:00 EDT；03-08 06:30Z=01:30 EST、
  07:30Z=03:30 EDT（同一学习日两个偏移）；周界 [03-02T05:00Z, 03-09T04:00Z)
  （起点 EST/终点 EDT，偏移跨边界改变）。
- 秋季 2026-11-01 02:00 EDT → 01:00 EST；05:30Z=01:30 EDT、06:30Z=01:30 EST
  （同一本地时刻出现两次，同一学习日两事件）。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import ErrorCode
from app.main import create_app
from infra.db.models import (
    Base,
    Card,
    CardDeletionBatch,
    LearningProject,
    PdfFile,
    ReviewEvent,
    ReviewState,
    User,
    UserPreferences,
)
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.stats.service import dashboard
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


# ---------- service 级（真实 SQLite + zoneinfo 分桶） ----------


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v25_stats.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)  # 周二（weekday=1）、ISO 周 33


def _seed_user(session: Session, *, user_id: str) -> None:
    if session.scalar(select(User).where(User.user_id == user_id)) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-01-01T00:00:00.000Z",
                updated_at="2026-01-01T00:00:00.000Z",
            )
        )
        session.flush()


def _seed_prefs(
    session: Session, *, user_id: str, tz: str = "Asia/Shanghai", daily_goal: int = 50
) -> None:
    """账号学习时区/每日目标（Task 3 权威偏好；默认行即 Shanghai/50）。"""
    session.add(
        UserPreferences(
            user_id=user_id,
            coverage_mode="BALANCED",
            basic_ratio=40,
            understanding_ratio=40,
            deep_question_ratio=20,
            daily_goal=daily_goal,
            learning_timezone=tz,
            current_project_id=None,
            updated_at="2026-01-01T00:00:00.000Z",
        )
    )


def _seed_deck(session: Session, *, user_id: str, name: str = "D") -> str:
    deck = create_deck(session, user_id=user_id, name=name, now="2026-01-01T00:00:00.000Z")
    session.flush()
    return deck.deck_id


def _seed_card(
    session: Session,
    *,
    user_id: str,
    deck_id: str,
    position: int,
    publication_state: str = "PUBLISHED",
    delete_batch_id: str | None = None,
) -> str:
    card = Card(
        card_id=_uuid(),
        deck_id=deck_id,
        user_id=user_id,
        source="MANUAL",
        position=position,
        front="f",
        back="b",
        card_type="QUESTION",
        publication_state=publication_state,
        delete_batch_id=delete_batch_id,
        version="v1",
        created_at="2026-01-01T00:00:00.000Z",
        updated_at="2026-01-01T00:00:00.000Z",
    )
    session.add(card)
    session.flush()
    return card.card_id


def _seed_event(
    session: Session,
    *,
    user_id: str,
    card_id: str,
    rating: str,
    reviewed_at: str,
    client_event_id: str | None = None,
) -> None:
    session.add(
        ReviewEvent(
            review_event_id=_uuid(),
            user_id=user_id,
            card_id=card_id,
            client_event_id=client_event_id or _uuid(),
            rating=rating,
            reviewed_at=reviewed_at,
            device_timezone=None,
            created_at=reviewed_at,
        )
    )


def _seed_review_state(
    session: Session, *, card_id: str, state: str = "NEW", stability: float = 0.0
) -> None:
    session.add(
        ReviewState(
            review_state_id=_uuid(),
            card_id=card_id,
            state=state,
            stability=stability,
            difficulty=5.0,
            due="2026-01-01T00:00:00.000Z",
            reps=1,
            lapses=0,
            updated_at="2026-01-01T00:00:00.000Z",
        )
    )


def test_dashboard_rating_count_vs_unique_card_semantics(
    session_factory: Callable[[], Session],
) -> None:
    """评级数 vs 唯一卡语义：weekly_total 计事件数，weekly_completed_count 计
    不同 (账号学习日期, card_id) 数——同卡同日多次评级只增事件数不增完成数。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        deck = _seed_deck(session, user_id=user)
        card = _seed_card(session, user_id=user, deck_id=deck, position=1)
        # 同卡 4 次评级：周一 09:00、周二 09:00、周二 10:00、周三 09:00（上海）
        for i, (rating, ts) in enumerate(
            [
                ("GOOD", "2026-08-10T01:00:00.000Z"),
                ("GOOD", "2026-08-11T01:00:00.000Z"),
                ("GOOD", "2026-08-11T02:00:00.000Z"),
                ("AGAIN", "2026-08-12T01:00:00.000Z"),
            ]
        ):
            _seed_event(session, user_id=user, card_id=card, rating=rating, reviewed_at=ts)
        session.commit()
    with session_factory() as session:
        result = dashboard(session, user_id=user, now=_now())
    assert result["weekly_total"] == 4  # 事件数
    assert result["weekly_activity"] == [1, 2, 1, 0, 0, 0, 0]  # 周一~周日
    assert result["weekly_completed_count"] == 3  # 3 个不同学习日（同卡同日去重）
    assert result["recall_accuracy"] == 0.75  # 3 GOOD / 4
    assert result["retention_rate"] == pytest.approx(2 / 3)  # 非首次 3 个（后 3 事件）
    assert result["first_answer_accuracy"] == 1.0  # 该卡首个事件 GOOD
    assert result["weekly_goal"] == 350  # 默认每日目标 50 × 7
    assert result["weekly_goal_progress"] == pytest.approx(3 / 350)


def test_dashboard_midnight_boundary_buckets_local_day(
    session_factory: Callable[[], Session],
) -> None:
    """午夜边界：UTC 15:59:59.999Z 仍是周一 23:59:59.999，16:00:00.000Z 已是周二
    00:00（上海 UTC+8）——按学习日分桶必须落在相邻两天。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        deck = _seed_deck(session, user_id=user)
        card = _seed_card(session, user_id=user, deck_id=deck, position=1)
        _seed_event(
            session,
            user_id=user,
            card_id=card,
            rating="GOOD",
            reviewed_at="2026-08-10T15:59:59.999Z",
        )
        _seed_event(
            session,
            user_id=user,
            card_id=card,
            rating="GOOD",
            reviewed_at="2026-08-10T16:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(session, user_id=user, now=_now())
    assert result["weekly_activity"] == [1, 1, 0, 0, 0, 0, 0]  # 周一 23:59 与周二 00:00
    assert result["weekly_completed_count"] == 2  # 两个学习日
    assert result["weekly_total"] == 2


def test_dashboard_dst_spring_forward_buckets_by_local_day(
    session_factory: Callable[[], Session],
) -> None:
    """DST 春季拨快（America/New_York 2026-03-08 02:00 EST→03:00 EDT）：同一学习日
    内偏移从 -05:00 变为 -04:00，两事件（01:30 EST 与 03:30 EDT）必须同落周日桶；
    周界起点 EST 终点 EDT（偏移跨边界改变）。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user, tz="America/New_York")
        deck = _seed_deck(session, user_id=user)
        c1 = _seed_card(session, user_id=user, deck_id=deck, position=1)
        c2 = _seed_card(session, user_id=user, deck_id=deck, position=2)
        _seed_event(
            session, user_id=user, card_id=c1, rating="GOOD", reviewed_at="2026-03-08T06:30:00.000Z"
        )  # 周日 01:30 EST
        _seed_event(
            session, user_id=user, card_id=c2, rating="GOOD", reviewed_at="2026-03-08T07:30:00.000Z"
        )  # 周日 03:30 EDT
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session,
            user_id=user,
            now=datetime(2026, 3, 2, 12, 0, 0, tzinfo=UTC),  # 周一 07:00 EST（转换周日在本周内）
        )
    assert result["timezone"] == "America/New_York"
    assert result["weekly_activity"] == [0, 0, 0, 0, 0, 0, 2]  # 两事件同落周日
    assert result["weekly_completed_count"] == 2  # 两卡各一学习日
    period = result["period"]
    assert isinstance(period, dict)
    # 周一起始：03-02 00:00 EST = 05:00Z；终点 03-09 00:00 EDT = 04:00Z
    assert period["start"] == "2026-03-02T05:00:00.000Z"
    assert period["end"] == "2026-03-09T04:00:00.000Z"
    assert period["week_ordinal"] == 10


def test_dashboard_dst_fall_back_repeated_local_hour_same_day(
    session_factory: Callable[[], Session],
) -> None:
    """DST 秋季拨回（America/New_York 2026-11-01 02:00 EDT→01:00 EST）：本地 01:30
    出现两次（EDT 与 EST），两事件同落周日学习日，不得双桶或漏桶。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user, tz="America/New_York")
        deck = _seed_deck(session, user_id=user)
        c1 = _seed_card(session, user_id=user, deck_id=deck, position=1)
        c2 = _seed_card(session, user_id=user, deck_id=deck, position=2)
        _seed_event(
            session, user_id=user, card_id=c1, rating="GOOD", reviewed_at="2026-11-01T05:30:00.000Z"
        )  # 周日 01:30 EDT
        _seed_event(
            session, user_id=user, card_id=c2, rating="GOOD", reviewed_at="2026-11-01T06:30:00.000Z"
        )  # 周日 01:30 EST（第二次）
        session.commit()
    with session_factory() as session:
        result = dashboard(
            session,
            user_id=user,
            now=datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC),  # 周一 08:00 EDT（转换周日在本周内）
        )
    assert result["weekly_activity"] == [0, 0, 0, 0, 0, 0, 2]
    assert result["weekly_completed_count"] == 2
    period = result["period"]
    assert isinstance(period, dict)
    # 周一起始：10-26 00:00 EDT = 04:00Z；终点 11-02 00:00 EST = 05:00Z（偏移反向改变）
    assert period["start"] == "2026-10-26T04:00:00.000Z"
    assert period["end"] == "2026-11-02T05:00:00.000Z"
    assert period["week_ordinal"] == 44


def test_dashboard_week_change_null_when_last_week_zero(
    session_factory: Callable[[], Session],
) -> None:
    """无上周分母：上周 0 事件 → week_change_rate null（客户端显示"暂无对比"），
    不得伪造 0% 或 100%。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        deck = _seed_deck(session, user_id=user)
        card = _seed_card(session, user_id=user, deck_id=deck, position=1)
        _seed_event(
            session,
            user_id=user,
            card_id=card,
            rating="GOOD",
            reviewed_at="2026-08-10T01:00:00.000Z",
        )
        session.commit()
    with session_factory() as session:
        result = dashboard(session, user_id=user, now=_now())
    assert result["week_change_rate"] is None
    assert result["weekly_total"] == 1


def test_dashboard_account_wide_across_decks_and_projects(
    session_factory: Callable[[], Session],
) -> None:
    """当前项目切换与独立牌组：统计为账号全量（不按项目/牌组过滤）——两个独立牌组
    的复习事件都计入；preferences.current_project_id 从 P1 切到 P2 不影响看板。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        # 两个项目（current_project_id FK → learning_projects → pdf_files）
        for i in range(2):
            session.add(
                PdfFile(
                    file_id=f"f-{i}",
                    user_id=user,
                    filename=f"p{i}.pdf",
                    storage_key=f"s{i}",
                    size_bytes=1,
                    status="PARSED",
                    created_at="2026-01-01T00:00:00.000Z",
                )
            )
        # 无 relationship 时 UoW 不保证插入顺序（test_projects_api 同款注释）——先落 pdf_files 行
        session.flush()
        for i in range(2):
            session.add(
                LearningProject(
                    project_id=f"proj-{i}",
                    user_id=user,
                    file_id=f"f-{i}",
                    name=f"P{i}",
                    version="v1",
                    created_at="2026-01-01T00:00:00.000Z",
                    updated_at="2026-01-01T00:00:00.000Z",
                )
            )
        session.flush()
        deck_a = _seed_deck(session, user_id=user, name="A")
        deck_b = _seed_deck(session, user_id=user, name="B")
        ca = _seed_card(session, user_id=user, deck_id=deck_a, position=1)
        cb = _seed_card(session, user_id=user, deck_id=deck_b, position=1)
        _seed_event(
            session, user_id=user, card_id=ca, rating="GOOD", reviewed_at="2026-08-10T01:00:00.000Z"
        )
        _seed_event(
            session, user_id=user, card_id=cb, rating="GOOD", reviewed_at="2026-08-11T01:00:00.000Z"
        )
        session.commit()
        prefs = session.get(UserPreferences, user)
        assert prefs is not None
        prefs.current_project_id = "proj-0"
        session.commit()
    with session_factory() as session:
        result_p1 = dashboard(session, user_id=user, now=_now())
        prefs = session.get(UserPreferences, user)
        assert prefs is not None
        prefs.current_project_id = "proj-1"  # 切换当前项目
        session.commit()
    with session_factory() as session:
        result_p2 = dashboard(session, user_id=user, now=_now())
    assert result_p1["weekly_total"] == 2  # 两个独立牌组都计入
    assert result_p2["weekly_total"] == 2  # 项目切换不影响账号全量统计
    assert result_p2["weekly_completed_count"] == 2


def test_dashboard_staged_and_delete_batch_cards_excluded(
    session_factory: Callable[[], Session],
) -> None:
    """STAGED 与删除批次卡排除（统一可见谓词）：不可见卡的评级事件不进入周活动/
    完成数/streak/首次答对率/已掌握（不保留幽灵计数）。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        deck = _seed_deck(session, user_id=user)
        visible = _seed_card(session, user_id=user, deck_id=deck, position=1)
        staged = _seed_card(
            session, user_id=user, deck_id=deck, position=2, publication_state="STAGED"
        )
        batch = CardDeletionBatch(
            delete_batch_id="db-1",
            user_id=user,
            status="PENDING",
            undo_until="2026-08-12T00:00:00.000Z",
            created_at="2026-08-01T00:00:00.000Z",
            updated_at="2026-08-01T00:00:00.000Z",
        )
        session.add(batch)
        session.flush()
        deleting = _seed_card(
            session, user_id=user, deck_id=deck, position=3, delete_batch_id="db-1"
        )
        # 可见卡：周一 GOOD、周二 AGAIN；已掌握
        _seed_event(
            session,
            user_id=user,
            card_id=visible,
            rating="GOOD",
            reviewed_at="2026-08-10T01:00:00.000Z",
        )
        _seed_event(
            session,
            user_id=user,
            card_id=visible,
            rating="AGAIN",
            reviewed_at="2026-08-11T01:00:00.000Z",
        )
        # STAGED 卡：周一 GOOD（不可见）
        _seed_event(
            session,
            user_id=user,
            card_id=staged,
            rating="GOOD",
            reviewed_at="2026-08-10T02:00:00.000Z",
        )
        # 删除批次卡：今天（周三）GOOD——若计入会延长 streak
        _seed_event(
            session,
            user_id=user,
            card_id=deleting,
            rating="GOOD",
            reviewed_at="2026-08-12T01:00:00.000Z",
        )
        _seed_review_state(session, card_id=visible, state="REVIEW", stability=25.0)
        _seed_review_state(session, card_id=staged, state="REVIEW", stability=30.0)
        _seed_review_state(session, card_id=deleting, state="REVIEW", stability=30.0)
        session.commit()
    with session_factory() as session:
        result = dashboard(session, user_id=user, now=_now())
    assert result["weekly_total"] == 2  # 仅可见卡 2 事件
    assert result["weekly_activity"] == [1, 1, 0, 0, 0, 0, 0]
    assert result["weekly_completed_count"] == 2
    assert result["recall_accuracy"] == 0.5
    assert result["first_answer_accuracy"] == 1.0  # 仅可见卡入分母
    assert result["mastered_card_count"] == 1  # STAGED/删除批次卡的 REVIEW 不计
    assert result["streak_days"] == 2  # 周三删除批次事件不得延长到 3


def test_dashboard_cross_user_isolation(session_factory: Callable[[], Session]) -> None:
    """跨用户隔离：他人事件（即使同时区同日期）不进入本用户看板。"""
    alice, bob = _uuid(), _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=alice)
        _seed_user(session, user_id=bob)
        _seed_prefs(session, user_id=alice)
        _seed_prefs(session, user_id=bob)
        deck_a = _seed_deck(session, user_id=alice, name="A")
        deck_b = _seed_deck(session, user_id=bob, name="B")
        ca = _seed_card(session, user_id=alice, deck_id=deck_a, position=1)
        cb = _seed_card(session, user_id=bob, deck_id=deck_b, position=1)
        _seed_event(
            session,
            user_id=alice,
            card_id=ca,
            rating="GOOD",
            reviewed_at="2026-08-10T01:00:00.000Z",
        )
        for rating in ("GOOD", "GOOD", "AGAIN"):
            _seed_event(
                session,
                user_id=bob,
                card_id=cb,
                rating=rating,
                reviewed_at="2026-08-10T02:00:00.000Z",
            )
        session.commit()
    with session_factory() as session:
        result = dashboard(session, user_id=alice, now=_now())
    assert result["weekly_total"] == 1  # bob 的 3 事件不计入
    assert result["weekly_completed_count"] == 1


def test_dashboard_timezone_change_rebuckets_utc_reviewed_at(
    session_factory: Callable[[], Session],
) -> None:
    """时区改变后重新分桶：UTC reviewed_at 不改写；同一事件在 Shanghai 落周二
    00:00（2026-08-10T16:00Z +8）、在 America/Los_Angeles 落周一 09:00（UTC-7）——
    学习日随账号时区改变。"""
    user = _uuid()
    event_ts = "2026-08-10T16:00:00.000Z"
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user, tz="Asia/Shanghai")
        deck = _seed_deck(session, user_id=user)
        card = _seed_card(session, user_id=user, deck_id=deck, position=1)
        _seed_event(session, user_id=user, card_id=card, rating="GOOD", reviewed_at=event_ts)
        session.commit()
    with session_factory() as session:
        result_sh = dashboard(session, user_id=user, now=_now())
        prefs = session.get(UserPreferences, user)
        assert prefs is not None
        prefs.learning_timezone = "America/Los_Angeles"  # 时区改变（不改写事件）
        session.commit()
    with session_factory() as session:
        result_la = dashboard(session, user_id=user, now=_now())
    activity_sh = result_sh["weekly_activity"]
    activity_la = result_la["weekly_activity"]
    assert isinstance(activity_sh, list) and isinstance(activity_la, list)
    assert activity_sh[1] == 1  # 上海：周二 00:00
    assert result_sh["weekly_completed_count"] == 1
    assert activity_la[0] == 1  # 洛杉矶：周一 09:00（UTC-7 折算）
    assert activity_la[1] == 0  # 该事件不再落周二
    assert result_la["weekly_completed_count"] == 1  # 学习日期随分桶改变
    assert result_la["timezone"] == "America/Los_Angeles"


# ---------- API 级（迁移后 schema + TestClient） ----------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "v25_stats_api.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _headers(client: TestClient) -> dict[str, str]:
    return auth_headers(client)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _make_deck(client: TestClient, headers: dict[str, str]) -> str:
    resp = client.post("/decks", json={"name": "统计"}, headers={**headers, **_idem()})
    assert resp.status_code == 201, resp.text
    return cast(str, resp.json()["deck_id"])


def _make_card(client: TestClient, headers: dict[str, str], deck_id: str) -> str:
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "q", "back": "a"},
        headers={**headers, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    return cast(str, resp.json()["card_id"])


def _rate(
    client: TestClient,
    headers: dict[str, str],
    card_id: str,
    rating: str,
    client_event_id: str,
) -> int:
    resp = client.post(
        "/review-events",
        json={"card_id": card_id, "rating": rating, "client_event_id": client_event_id},
        headers={**headers, **_idem()},
    )
    return int(resp.status_code)


def test_dashboard_api_server_derives_timezone_and_goal(client: TestClient) -> None:
    """无客户端参数：服务端按账号偏好派生 timezone 与 weekly_goal（daily_goal × 7）；
    device_timezone 不再上报也不参与分桶。"""
    headers = _headers(client)
    resp = client.patch(
        "/preferences",
        headers={**headers, **_idem()},
        json={"learning_timezone": "America/New_York", "daily_learning_goal": 70},
    )
    assert resp.status_code == 200, resp.text
    deck = _make_deck(client, headers)
    card = _make_card(client, headers, deck)
    assert _rate(client, headers, card, "GOOD", str(uuid.uuid4())) == 200
    resp = client.get("/stats/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["timezone"] == "America/New_York"
    assert body["weekly_goal"] == 490  # 70 × 7
    assert body["weekly_total"] == 1
    assert body["weekly_goal_progress"] == pytest.approx(1 / 490)
    assert body["has_data"] is True
    assert body["period"]["week_ordinal"] == datetime.now(UTC).isocalendar()[1]


def test_dashboard_api_unknown_query_params_rejected(client: TestClient) -> None:
    """V2.4 遗留客户端参数移除：timezone/weekly_goal/任意未知参数 → 400 VALIDATION_ERROR。"""
    headers = _headers(client)
    for params in ("timezone=Asia/Shanghai", "weekly_goal=50", "foo=1"):
        resp = client.get(f"/stats/dashboard?{params}", headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR
    # 无参数仍可用
    resp = client.get("/stats/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text


def test_dashboard_api_repeated_rating_same_client_event_id_counts_once(
    client: TestClient,
) -> None:
    """重复评级：同 client_event_id 重放（幂等兜底）不重复计数——2 次提交只 1 事件。"""
    headers = _headers(client)
    deck = _make_deck(client, headers)
    card = _make_card(client, headers, deck)
    client_event_id = str(uuid.uuid4())
    assert _rate(client, headers, card, "GOOD", client_event_id) == 200
    assert _rate(client, headers, card, "GOOD", client_event_id) == 200  # 重放
    resp = client.get("/stats/dashboard", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["weekly_total"] == 1
    assert resp.json()["weekly_completed_count"] == 1
    assert resp.json()["recall_accuracy"] == 1.0


def test_dashboard_api_free_browse_not_counted(client: TestClient) -> None:
    """自由刷题不计统计：只浏览（GET /decks/{id}/cards）不产生事件；评级后浏览也不增。"""
    headers = _headers(client)
    deck = _make_deck(client, headers)
    card = _make_card(client, headers, deck)
    resp = client.get(f"/decks/{deck}/cards", headers=headers)
    assert resp.status_code == 200, resp.text
    assert client.get("/stats/dashboard", headers=headers).json()["weekly_total"] == 0
    assert _rate(client, headers, card, "GOOD", str(uuid.uuid4())) == 200
    assert client.get("/stats/dashboard", headers=headers).json()["weekly_total"] == 1
    resp = client.get(f"/decks/{deck}/cards", headers=headers)  # 再浏览
    assert resp.status_code == 200, resp.text
    assert client.get("/stats/dashboard", headers=headers).json()["weekly_total"] == 1
