"""V2.5 看板聚合性能证据（Task 11；database-design §4 基线：千级卡片、万级事件）。

按 Architecture 基线数据量播种（脚本级种子，不入库）：1000 卡 + 10000 复习事件 +
1000 review_states（约 300 张已掌握），事件分布在当前周/上周/更早（近 28 天），
实测命名查询 dashboard()（周窗口区间查询 + 全历史首事件遍历 + 已掌握 COUNT）。
记录数据集规模、环境、命令与逐次 elapsed/min/median/max（n=10 时 P95≈max），
禁止从空库宣称性能；断言宽松上限（CI 抖动余量），实测值见 -s 输出与报告。
"""

import os
import platform
import statistics
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from infra.db.models import Base, Card, ReviewEvent, ReviewState, User, UserPreferences
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck
from services.stats.service import dashboard

CARDS = 1000
EVENTS_PER_CARD = 10  # 万级事件（10000）
MASTERED_CARDS = 300  # REVIEW 且 stability>=21
_NOW = datetime(2026, 8, 11, 10, 0, 0, tzinfo=UTC)  # 周二；上海周界 8/09T16:00Z~8/16T16:00Z


@pytest.fixture
def seeded_session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'perf.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    user_id = str(uuid.uuid4())
    with factory() as session:
        session.add(
            User(
                user_id=user_id,
                username="perf-user",
                email="perf-user@example.com",
                password_hash="x",
                created_at="2026-01-01T00:00:00.000Z",
                updated_at="2026-01-01T00:00:00.000Z",
            )
        )
        session.add(
            UserPreferences(
                user_id=user_id,
                coverage_mode="BALANCED",
                basic_ratio=40,
                understanding_ratio=40,
                deep_question_ratio=20,
                daily_goal=50,
                learning_timezone="Asia/Shanghai",
                current_project_id=None,
                updated_at="2026-01-01T00:00:00.000Z",
            )
        )
        # 无 relationship 时 UoW 不保证插入顺序——父行先 flush（test_projects_api 同款注释）
        session.flush()
        deck = create_deck(session, user_id=user_id, name="PERF", now="2026-01-01T00:00:00.000Z")
        session.flush()
        # 卡片 + 初始排程行（全量 add_all 后单次 commit，播种不在计时内）
        cards: list[Card] = []
        states: list[ReviewState] = []
        events: list[ReviewEvent] = []
        base = _NOW
        for i in range(CARDS):
            card_id = str(uuid.uuid4())
            cards.append(
                Card(
                    card_id=card_id,
                    deck_id=deck.deck_id,
                    user_id=user_id,
                    source="MANUAL",
                    position=i + 1,
                    front="f",
                    back="b",
                    card_type="QUESTION",
                    version="v1",
                    created_at="2026-01-01T00:00:00.000Z",
                    updated_at="2026-01-01T00:00:00.000Z",
                )
            )
            # 确定性事件时间：近 28 天窗口内（约 1/4 落当前周、1/4 上周、其余更早）
            for j in range(EVENTS_PER_CARD):
                ts = base - timedelta(
                    days=(i * 7 + j * 3) % 28,
                    hours=(i + j * 5) % 24,
                    minutes=(i * 13 + j * 7) % 60,
                )
                events.append(
                    ReviewEvent(
                        review_event_id=str(uuid.uuid4()),
                        user_id=user_id,
                        card_id=card_id,
                        client_event_id=str(uuid.uuid4()),
                        rating=("GOOD", "AGAIN", "HARD", "EASY")[(i + j) % 4],
                        reviewed_at=ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        device_timezone=None,
                        created_at=ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    )
                )
            if i < MASTERED_CARDS:
                states.append(
                    ReviewState(
                        review_state_id=str(uuid.uuid4()),
                        card_id=card_id,
                        state="REVIEW",
                        stability=30.0,
                        difficulty=5.0,
                        due="2026-01-01T00:00:00.000Z",
                        reps=5,
                        lapses=0,
                        updated_at="2026-01-01T00:00:00.000Z",
                    )
                )
        session.add_all(cards)
        session.flush()  # 事件 FK → cards：卡片行先落库
        session.add_all(states)
        session.add_all(events)
        session.commit()
    return factory


@pytest.mark.perf
def test_dashboard_aggregation_within_baseline_volume(
    seeded_session_factory: Callable[[], Session],
) -> None:
    """千级卡片、万级事件下看板聚合实测：记录 elapsed 证据并断言宽松上限。"""
    with seeded_session_factory() as session:
        prefs = session.scalar(select(UserPreferences))
        assert prefs is not None
        user_id = prefs.user_id
        deck = session.scalar(select(Card).where(Card.user_id == user_id))
        assert deck is not None
        total_events = session.scalar(select(func.count(ReviewEvent.review_event_id)))
        # 基线数据量自检（种子与设计一致，防止播种退化后"空库宣称性能"）
        assert total_events == CARDS * EVENTS_PER_CARD
        # 预热（解析/缓存路径稳定后计时）
        dashboard(session, user_id=user_id, now=_NOW)
        samples: list[float] = []
        for _ in range(10):
            t0 = time.perf_counter()
            dashboard(session, user_id=user_id, now=_NOW)
            samples.append(time.perf_counter() - t0)
    env = (
        f"{platform.system()} {platform.release()} | CPU {os.cpu_count()} | "
        f"python {platform.python_version()}"
    )
    evidence = {
        "dataset": f"{CARDS} cards / {total_events} events / {MASTERED_CARDS} review_states "
        f"(SQLite file)",
        "environment": env,
        "command": "python -m pytest tests/integration/test_v25_stats_performance.py -s -q",
        "samples_elapsed_s": [round(s, 4) for s in samples],
        "min_s": round(min(samples), 4),
        "median_s": round(statistics.median(samples), 4),
        "max_s": round(max(samples), 4),
        "p95_s_approx_max": round(max(samples), 4),  # n=10：P95 ≈ max
    }
    print("\n[PERF] " + repr(evidence))
    assert statistics.median(samples) < 3.0, f"median {statistics.median(samples):.3f}s 超上限"
