"""学习会话与评分来源集成测试（structure-contract 3.25/3.11/6.6；V25-D-37）。

HTTP 级（client fixture，迁移后 schema）：
- begin 自然键当日续用（同日同来源同范围返回同一行并继承累计秒数）；
- begin/report 幂等键强制与同键重放一致；report 绝对值 max 合并（低值不回退）；
- 范围校验：ADHOC 必带归属自己的 deck（缺 → 400、跨用户 → 404）；PLAN/BACKLOG 带
  deck → 400；origin 非法 → 400；study_seconds 越界 → 400；未知会话 → 404；
- 列表：缺省 = 账号学习时区今天；显式 study_date 格式校验。

service 级（session_factory，直接控制 now）：
- 跨学习日新行（同来源同范围隔天 begin → 新 session_id/新 study_date）；
- 评分 origin 落库（缺省 NULL=未分类、合法值原样、非法值 VALIDATION_ERROR）；
- dashboard 学习时长聚合（daily 7 桶 + 周合计 + 三来源拆分）。
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.main import create_app
from infra.db.models import (
    Base,
    Card,
    ReviewEvent,
    StudySession,
    User,
    UserPreferences,
)
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.review.service import submit_review
from services.stats.service import dashboard
from services.study.sessions import begin_or_resume, report_study_session
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


# ---------- HTTP 级 ----------


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "study_sessions.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _deck(client: TestClient, user: dict[str, str]) -> str:
    r = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()})
    assert r.status_code == 201, r.text
    return cast(str, r.json()["deck_id"])


def _card(client: TestClient, user: dict[str, str], deck_id: str) -> str:
    r = client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**user, **_idem()}
    )
    assert r.status_code == 201, r.text
    return cast(str, r.json()["card_id"])


def _begin(client: TestClient, user: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    r = client.post("/study/sessions", json=body, headers={**user, **_idem()})
    assert r.status_code == 200, r.text
    return cast(dict[str, Any], r.json())


def _report(
    client: TestClient,
    user: dict[str, str],
    session_id: str,
    study_seconds: int,
    *,
    ended: bool = False,
) -> dict[str, Any]:
    r = client.patch(
        f"/study/sessions/{session_id}",
        json={"study_seconds": study_seconds, "ended": ended},
        headers={**user, **_idem()},
    )
    assert r.status_code == 200, r.text
    return cast(dict[str, Any], r.json())


def test_begin_same_day_resumes_same_row_with_accumulated_seconds(
    client: TestClient,
) -> None:
    """当日续学：同日同来源同范围再次 begin 返回同一 session_id 并继承累计秒数。"""
    headers = _user(client)
    first = _begin(client, headers, {"origin": "PLAN"})
    assert first["study_seconds"] == 0
    assert first["ended_at"] is None

    reported = _report(client, headers, first["session_id"], 300)
    assert reported["study_seconds"] == 300

    resumed = _begin(client, headers, {"origin": "PLAN"})
    assert resumed["session_id"] == first["session_id"]
    assert resumed["study_seconds"] == 300  # 续学基数 = 服务端已累计值


def test_begin_distinct_scopes_create_distinct_rows(client: TestClient) -> None:
    """同日不同来源/范围为不同会话（PLAN、BACKLOG、ADHOC deck 三行互异）。"""
    headers = _user(client)
    deck_id = _deck(client, headers)
    plan = _begin(client, headers, {"origin": "PLAN"})
    backlog = _begin(client, headers, {"origin": "BACKLOG"})
    adhoc = _begin(client, headers, {"origin": "ADHOC", "deck_id": deck_id})
    ids = {plan["session_id"], backlog["session_id"], adhoc["session_id"]}
    assert len(ids) == 3
    assert plan["deck_id"] is None and backlog["deck_id"] is None
    assert adhoc["deck_id"] == deck_id

    items = client.get("/study/sessions", headers=headers).json()["items"]
    assert {item["session_id"] for item in items} == ids


def test_begin_scope_validation(client: TestClient) -> None:
    """范围校验：ADHOC 缺 deck 400；PLAN/BACKLOG 带 deck 400；非法 origin 400。"""
    headers = _user(client)
    deck_id = _deck(client, headers)

    r = client.post("/study/sessions", json={"origin": "ADHOC"}, headers={**headers, **_idem()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    for origin in ("PLAN", "BACKLOG"):
        r = client.post(
            "/study/sessions",
            json={"origin": origin, "deck_id": deck_id},
            headers={**headers, **_idem()},
        )
        assert r.status_code == 400, (origin, r.text)

    r = client.post("/study/sessions", json={"origin": "BOGUS"}, headers={**headers, **_idem()})
    assert r.status_code == 400


def test_begin_adhoc_requires_owned_deck(client: TestClient) -> None:
    """ADHOC 跨用户卡组 → 404 DECK_NOT_FOUND（不暴露存在性）。"""
    alice = _user(client, "alice")
    bob = _user(client, "bob")
    bob_deck = _deck(client, bob)
    r = client.post(
        "/study/sessions",
        json={"origin": "ADHOC", "deck_id": bob_deck},
        headers={**alice, **_idem()},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_begin_requires_idempotency_key_and_replays(client: TestClient) -> None:
    """幂等键强制 + 同键同体重放返回一致快照。"""
    headers = _user(client)
    r = client.post("/study/sessions", json={"origin": "PLAN"}, headers=headers)
    assert r.status_code == 400  # 缺 Idempotency-Key

    key = _idem()
    body = {"origin": "PLAN"}
    r1 = client.post("/study/sessions", json=body, headers={**headers, **key})
    r2 = client.post("/study/sessions", json=body, headers={**headers, **key})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()


def test_report_absolute_max_merge_and_end(client: TestClient) -> None:
    """绝对值 max 合并：低值不回退、高值前进；ended=True 写结束时间但不阻断续用。"""
    headers = _user(client)
    session = _begin(client, headers, {"origin": "BACKLOG"})
    sid = session["session_id"]

    assert _report(client, headers, sid, 300)["study_seconds"] == 300
    assert _report(client, headers, sid, 200)["study_seconds"] == 300  # 乱序低值不回退
    ended = _report(client, headers, sid, 500, ended=True)
    assert ended["study_seconds"] == 500
    assert ended["ended_at"] is not None

    resumed = _begin(client, headers, {"origin": "BACKLOG"})  # 同日续用不受 ended 影响
    assert resumed["session_id"] == sid
    assert resumed["study_seconds"] == 500


def test_report_validates_range_and_unknown_session(client: TestClient) -> None:
    """study_seconds 越界 → 400；未知会话 → 404 SESSION_NOT_FOUND。"""
    headers = _user(client)
    session = _begin(client, headers, {"origin": "PLAN"})
    for bad in (-1, 86401):
        r = client.patch(
            f"/study/sessions/{session['session_id']}",
            json={"study_seconds": bad},
            headers={**headers, **_idem()},
        )
        assert r.status_code == 400, (bad, r.text)

    r = client.patch(
        f"/study/sessions/{uuid.uuid4()}",
        json={"study_seconds": 10},
        headers={**headers, **_idem()},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SESSION_NOT_FOUND"


def test_summary_aggregates_per_deck_and_per_origin(client: TestClient) -> None:
    """全历史汇总：ADHOC 按卡组聚合 + 三来源合计；跨用户隔离。"""
    alice = _user(client, "alice")
    deck_a = _deck(client, alice)
    _deck(client, alice)  # 第二个卡组不产生会话，不出现在汇总里

    adhoc = _begin(client, alice, {"origin": "ADHOC", "deck_id": deck_a})
    _report(client, alice, adhoc["session_id"], 300)
    plan = _begin(client, alice, {"origin": "PLAN"})
    _report(client, alice, plan["session_id"], 120, ended=True)

    r = client.get("/study/sessions/summary", headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deck_study_seconds"] == [{"deck_id": deck_a, "study_seconds": 300}]
    assert body["plan_study_seconds"] == 120
    assert body["backlog_study_seconds"] == 0
    assert body["adhoc_study_seconds"] == 300

    # 他人会话不串账
    bob = _user(client, "bob")
    bob_body = client.get("/study/sessions/summary", headers=bob).json()
    assert bob_body["deck_study_seconds"] == []
    assert bob_body["plan_study_seconds"] == 0


def test_list_sessions_validates_study_date(client: TestClient) -> None:
    """显式 study_date 格式校验：非 yyyy-MM-dd / 非法日期 → 400。"""
    headers = _user(client)
    for bad in ("2026/09/17", "2026-13-01", "not-a-date"):
        r = client.get("/study/sessions", params={"study_date": bad}, headers=headers)
        assert r.status_code == 400, (bad, r.text)
    assert client.get("/study/sessions", headers=headers).status_code == 200


def test_reset_seals_old_session_and_returns_review_all_queue(client: TestClient) -> None:
    """会话重置：旧会话封存（时长保留）、新行从 0 计时；队列 = 新卡最前 + 其余按风险。"""
    headers = _user(client)
    deck_id = _deck(client, headers)
    # 卡 1：已评级（遗忘风险高、下午复盘该排前）；卡 2：新卡（应排最前）。
    card_rated = _card(client, headers, deck_id)
    card_new = _card(client, headers, deck_id)
    r = client.post(
        "/review-events",
        json={
            "card_id": card_rated,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
        },
        headers={**headers, **_idem()},
    )
    assert r.status_code == 200, r.text

    first = _begin(client, headers, {"origin": "ADHOC", "deck_id": deck_id})
    _report(client, headers, first["session_id"], 300)

    r = client.post(
        "/study/sessions",
        json={"origin": "ADHOC", "deck_id": deck_id, "reset": True},
        headers={**headers, **_idem()},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] != first["session_id"]
    assert body["study_seconds"] == 0  # 新一代会话从 0 计时
    assert body["ended_at"] is None
    assert [c["card_id"] for c in body["cards"]] == [card_new, card_rated]  # 新卡最前

    # 续用（无 reset）命中的是新活跃行，且上午的时长仍在历史里。
    resumed = _begin(client, headers, {"origin": "ADHOC", "deck_id": deck_id})
    assert resumed["session_id"] == body["session_id"]
    assert resumed["study_seconds"] == 0
    items = client.get("/study/sessions", headers=headers).json()["items"]
    sealed = next(i for i in items if i["session_id"] == first["session_id"])
    assert sealed["study_seconds"] == 300
    assert sealed["ended_at"] is not None

    # summary 的卡组合计包含封存代 + 活跃代。
    summary = client.get("/study/sessions/summary", headers=headers).json()
    deck_total = next(
        d["study_seconds"] for d in summary["deck_study_seconds"] if d["deck_id"] == deck_id
    )
    assert deck_total == 300


def test_reset_requires_adhoc_deck(client: TestClient) -> None:
    """reset 只对单卡组会话有意义：PLAN/BACKLOG 携带 → 400。"""
    headers = _user(client)
    r = client.post(
        "/study/sessions",
        json={"origin": "PLAN", "reset": True},
        headers={**headers, **_idem()},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_review_events_accepts_optional_origin(client: TestClient) -> None:
    """评分来源字段：合法值 200；缺省 200（旧客户端过渡期）；非法值 400。"""
    headers = _user(client)
    deck_id = _deck(client, headers)
    card_id = _card(client, headers, deck_id)

    def _submit(origin: str | None) -> int:
        body: dict[str, Any] = {
            "card_id": card_id,
            "rating": "GOOD",
            "client_event_id": str(uuid.uuid4()),
        }
        if origin is not None:
            body["origin"] = origin
        r = client.post("/review-events", json=body, headers={**headers, **_idem()})
        return int(r.status_code)

    # 同卡多次评分：AGAIN 后再 GOOD，避免 AGAIN 推回到期影响后续（这里只断言状态码）
    assert _submit("PLAN") == 200
    assert _submit("ADHOC") == 200
    assert _submit(None) == 200
    assert _submit("BOGUS") == 400


# ---------- service 级 ----------


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'study_sessions_svc.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_user(session: Session, *, user_id: str) -> None:
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


def _seed_prefs(session: Session, *, user_id: str, tz: str = "Asia/Shanghai") -> None:
    session.add(
        UserPreferences(
            user_id=user_id,
            coverage_mode="BALANCED",
            basic_ratio=40,
            understanding_ratio=40,
            deep_question_ratio=20,
            daily_goal=50,
            learning_timezone=tz,
            current_project_id=None,
            updated_at="2026-01-01T00:00:00.000Z",
        )
    )
    session.flush()


def _seed_deck(session: Session, *, user_id: str) -> str:
    deck = create_deck(session, user_id=user_id, name="D", now="2026-01-01T00:00:00.000Z")
    session.flush()
    return deck.deck_id


def _seed_card(session: Session, *, user_id: str, deck_id: str) -> str:
    card_id = _uuid()
    session.add(
        Card(
            card_id=card_id,
            deck_id=deck_id,
            user_id=user_id,
            source="MANUAL",
            position=1,
            front="f",
            back="b",
            card_type="QUESTION",
            publication_state="PUBLISHED",
            version="v1",
            created_at="2026-01-01T00:00:00.000Z",
            updated_at="2026-01-01T00:00:00.000Z",
        )
    )
    session.flush()
    return card_id


def test_begin_cross_day_opens_new_row(session_factory: Callable[[], Session]) -> None:
    """跨学习日新行：Asia/Shanghai 下 2026-08-10T16:00Z 与次日 16:00Z 分属两个学习日。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        day1 = begin_or_resume(
            session, user_id=user, origin="PLAN", deck_id=None, now="2026-08-10T16:00:00.000Z"
        )
        report_study_session(
            session,
            user_id=user,
            session_id=str(day1["session_id"]),
            study_seconds=120,
            ended=True,
            now="2026-08-10T16:30:00.000Z",
        )
        session.commit()

    with session_factory() as session:
        day2 = begin_or_resume(
            session, user_id=user, origin="PLAN", deck_id=None, now="2026-08-11T16:00:00.000Z"
        )
        assert day2["session_id"] != day1["session_id"]  # 跨天自动新会话
        assert day2["study_date"] == "2026-08-12"
        assert day2["study_seconds"] == 0  # 不继承隔天时长
        assert day1["study_date"] == "2026-08-11"


def test_review_origin_persisted_and_validated(session_factory: Callable[[], Session]) -> None:
    """评分 origin 落库：合法值原样、缺省 NULL=未分类、非法值 VALIDATION_ERROR。"""
    user = _uuid()
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        deck_id = _seed_deck(session, user_id=user)
        card_id = _seed_card(session, user_id=user, deck_id=deck_id)
        session.commit()

    with session_factory() as session:
        submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=str(uuid.uuid4()),
            device_timezone=None,
            origin="ADHOC",
            now="2026-08-10T10:00:00.000Z",
        )
        submit_review(
            session,
            user_id=user,
            card_id=card_id,
            rating="GOOD",
            client_event_id=str(uuid.uuid4()),
            device_timezone=None,
            origin=None,
            now="2026-08-10T10:01:00.000Z",
        )
        session.commit()

    with session_factory() as session:
        origins = list(
            session.scalars(select(ReviewEvent.origin).where(ReviewEvent.user_id == user)).all()
        )
        assert origins == ["ADHOC", None]

        with pytest.raises(AppError) as excinfo:
            submit_review(
                session,
                user_id=user,
                card_id=card_id,
                rating="GOOD",
                client_event_id=str(uuid.uuid4()),
                device_timezone=None,
                origin="BOGUS",
                now="2026-08-10T10:02:00.000Z",
            )
        assert excinfo.value.code == ErrorCode.VALIDATION_ERROR


def test_dashboard_aggregates_study_seconds_by_day_and_origin(
    session_factory: Callable[[], Session],
) -> None:
    """看板时长聚合：daily 7 桶（学习日对齐）+ 周合计 + PLAN/BACKLOG/ADHOC 拆分；
    窗口外学习日不计入。"""
    user = _uuid()
    now = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)  # 周二（ISO 周 33：08-10 周一）
    with session_factory() as session:
        _seed_user(session, user_id=user)
        _seed_prefs(session, user_id=user)
        rows = [
            ("2026-08-10", "PLAN", 600),  # 周一
            ("2026-08-10", "ADHOC", 120),
            ("2026-08-11", "BACKLOG", 300),  # 周二（今天）
            ("2026-08-09", "PLAN", 999),  # 上周日：窗口外不计
        ]
        for study_date, origin, seconds in rows:
            session.add(
                StudySession(
                    session_id=_uuid(),
                    user_id=user,
                    origin=origin,
                    deck_id=None,
                    deck_key="*",
                    study_date=study_date,
                    study_seconds=seconds,
                    started_at=f"{study_date}T02:00:00.000Z",
                )
            )
        session.commit()
        result = dashboard(session, user_id=user, now=now)

    assert result["daily_study_seconds"] == [720, 300, 0, 0, 0, 0, 0]
    assert result["weekly_study_seconds"] == 1020
    assert result["plan_study_seconds"] == 600
    assert result["backlog_study_seconds"] == 300
    assert result["adhoc_study_seconds"] == 120
