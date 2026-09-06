"""API 端点延迟基线（structure-contract 8.6；G4 性能闸门证据出口）。

按验收统一基线数据量（V25-REL-FR-05：千级卡片、万级复习事件）播种后，经真实
HTTP 栈（TestClient + 迁移库）采样代表性读/写端点，逐端点输出 [PERF] 实测
（n=20，P95≈max），格式同 test_v25_stats_performance 观测段。断言宽松中位数
上限（CI 抖动余量）；§8.6 初始参考值（读 p95≤500ms / 写 p95≤800ms）是运维侧
对照目标，不以单次 CI 采样硬判（校准纪律见 structure-contract 8.6）。
"""

import os
import platform
import statistics
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.models import Card, ReviewEvent, ReviewState, UserPreferences
from infra.db.session import create_db_engine, create_session_factory
from tests.conftest import auth_headers

CARDS = 1000
EVENTS_PER_CARD = 10  # 万级事件（V25-REL-FR-05 基线）
MASTERED_CARDS = 300  # REVIEW 且 stability>=21（dashboard 已掌握计数口径）
SAMPLING_N = 20


def _upgrade(db_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


@pytest.fixture
def baseline_ctx(tmp_path: Path) -> tuple[TestClient, dict[str, str], str, list[str]]:
    """迁移库 + HTTP 注册用户 + 千卡万事件播种；返回 (client, headers, deck_id, card_ids)。"""
    db_path = tmp_path / "latency.db"
    _upgrade(db_path)
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
        rate_limit_ip_burst=1000,
        rate_limit_write_per_minute=100000,
    )
    app = create_app(settings)
    client = TestClient(app)
    headers = auth_headers(client, username="latency", password="secret-pass-1")

    write_headers = {**headers, "Idempotency-Key": str(uuid.uuid4())}
    r = client.post("/decks", json={"name": "延迟基线牌组"}, headers=write_headers)
    assert r.status_code == 201, r.text
    deck_id = r.json()["deck_id"]

    engine = create_db_engine(f"sqlite:///{db_path}")
    user_id = (
        engine.connect()
        .execute(text("SELECT user_id FROM users WHERE username = 'latency'"))
        .scalar_one()
    )
    factory = create_session_factory(engine)
    card_ids: list[str] = []
    with factory() as session:
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
        session.flush()
        cards: list[Card] = []
        states: list[ReviewState] = []
        events: list[ReviewEvent] = []
        base = datetime.now(UTC)  # 事件相对当前时间分布（dashboard 周窗口为 now 相对口径）
        for i in range(CARDS):
            card_id = str(uuid.uuid4())
            card_ids.append(card_id)
            cards.append(
                Card(
                    card_id=card_id,
                    deck_id=deck_id,
                    user_id=user_id,
                    source="MANUAL",
                    position=i + 1,
                    front=f"front-{i}",
                    back="back",
                    card_type="QUESTION",
                    version="v1",
                    created_at="2026-01-01T00:00:00.000Z",
                    updated_at="2026-01-01T00:00:00.000Z",
                )
            )
            for j in range(EVENTS_PER_CARD):
                # 确定性事件时间：近 28 天窗口内（约 1/4 当前周、1/4 上周、其余更早）
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
    return client, headers, deck_id, card_ids


def _sample_endpoint(fn: Callable[[], httpx.Response], label: str) -> list[float]:
    """预热 1 次并校验状态码，随后 n 次计时采样（响应校验在计时窗口外）。"""
    probe = fn()
    assert probe.status_code < 400, f"{label} 预热失败: {probe.status_code} {probe.text[:200]}"
    samples: list[float] = []
    for _ in range(SAMPLING_N):
        t0 = time.perf_counter()
        response = fn()
        elapsed = time.perf_counter() - t0
        assert response.status_code < 400, f"{label} 采样失败: {response.status_code}"
        samples.append(elapsed)
    return samples


@pytest.mark.perf
def test_api_latency_baseline_reads_and_writes(
    baseline_ctx: tuple[TestClient, dict[str, str], str, list[str]],
) -> None:
    """代表性端点在基线数据量下的延迟实测：[PERF] 证据 + 宽松中位数上限。"""
    client, headers, deck_id, card_ids = baseline_ctx

    def _get(path: str) -> Callable[[], httpx.Response]:
        return lambda: client.get(path, headers=headers)

    def _post_review() -> httpx.Response:
        return cast(
            httpx.Response,
            client.post(
                "/review-events",
                json={
                    "card_id": card_ids[0],
                    "rating": "GOOD",
                    "client_event_id": str(uuid.uuid4()),
                },
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            ),
        )

    endpoints: list[tuple[str, Callable[[], httpx.Response], float]] = [
        ("GET /decks", _get("/decks"), 1.5),
        ("GET /decks/{deck_id}/cards", _get(f"/decks/{deck_id}/cards"), 1.5),
        ("GET /stats/dashboard", _get("/stats/dashboard"), 3.0),
        ("GET /study/today", _get("/study/today"), 1.5),
        ("POST /review-events", _post_review, 1.0),
    ]

    evidence: dict[str, object] = {
        "dataset": f"{CARDS} cards / {CARDS * EVENTS_PER_CARD} events / "
        f"{MASTERED_CARDS} review_states (SQLite file, HTTP TestClient)",
        "environment": (
            f"{platform.system()} {platform.release()} | CPU {os.cpu_count()} | "
            f"python {platform.python_version()}"
        ),
        "command": "python -m pytest tests/integration/test_api_latency_baseline.py -s -q",
        "sampling_n": SAMPLING_N,
        "endpoints": {},
    }
    for label, fn, median_budget in endpoints:
        samples = _sample_endpoint(fn, label)
        evidence["endpoints"][label] = {  # type: ignore[index]
            "samples_elapsed_s": [round(s, 4) for s in samples],
            "min_s": round(min(samples), 4),
            "p50_s": round(statistics.median(samples), 4),
            "max_s_p95_approx": round(max(samples), 4),
        }
        assert statistics.median(samples) < median_budget, (
            f"{label} median {statistics.median(samples):.3f}s 超宽松上限 {median_budget}s"
        )
    print("\n[PERF] " + repr(evidence))
