"""自由刷题（structure-contract 6.5；openapi GET /decks/{deck_id}/cards）集成测试。

自由刷题 = 只浏览：不创建复习事件、不改变排程、不计今日完成数（V25-STUDY-FR-10）。
筛选：order=position|random；content_difficulty=BASIC|UNDERSTANDING|DEEP_QUESTION|UNLABELED；
mastery=all|mastered|unmastered。随机顺序由客户端会话 seed 固定——服务端确定性伪随机
（seed = user_id+deck_id 哈希），同一用户同一牌组每次请求同序，不得每翻一张重新洗牌。

决策记录（实现裁决，报告同步）：openapi 无 seed 查询参数，客户端无法上传会话 seed；
服务端以 (user_id, deck_id) 派生确定性排列满足"会话稳定、不逐次洗牌"。
"""

import uuid
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "browse.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
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


def _make_deck(client: TestClient, user: dict[str, str], name: str = "D") -> str:
    return cast(
        str,
        client.post("/decks", json={"name": name}, headers={**user, **_idem()}).json()["deck_id"],
    )


def _make_card(client: TestClient, user: dict[str, str], deck_id: str, front: str) -> str:
    return cast(
        str,
        client.post(
            f"/decks/{deck_id}/cards",
            json={"front": front, "back": "b"},
            headers={**user, **_idem()},
        ).json()["card_id"],
    )


def _user_id(client: TestClient, tmp_path: Path, username: str = "alice") -> str:
    """注册用户的 user_id（users 表按 username 查询；与 client 同一临时库）。"""
    import sqlalchemy

    from infra.db.session import create_db_engine

    db_file = next(tmp_path.glob("*.db"))
    engine = create_db_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        row = conn.execute(
            sqlalchemy.text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    engine.dispose()
    assert row is not None
    return cast(str, row)


def _seed_card_with_state(
    tmp_path: Path,
    *,
    user_id: str,
    deck_id: str,
    position: int,
    state: str = "NEW",
    stability: float = 0.0,
    target_difficulty: str | None = None,
    front: str = "f",
    publication_state: str = "PUBLISHED",
    delete_batch_id: str | None = None,
) -> str:
    """ORM 直种卡片 + 排程状态（target_difficulty/mastery 无法经创建端点设置）。"""
    import uuid as _uuid

    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Card, CardDeletionBatch, ReviewState
    from infra.db.session import create_db_engine

    now = "2026-08-10T00:00:00.000Z"
    card_id = str(_uuid.uuid4())
    db_file = next(tmp_path.glob("*.db"))
    engine = create_db_engine(f"sqlite:///{db_file}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as session:
        if delete_batch_id is not None:
            session.add(
                CardDeletionBatch(  # FK：删除批次行须存在（Task 8 语义：批次即删除证据）
                    delete_batch_id=delete_batch_id,
                    user_id=user_id,
                    status="PENDING",
                    undo_until=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()  # unitofwork 不保证批次先于卡插入——显式落批次行（Task 8 同款）
        session.add(
            Card(
                card_id=card_id,
                deck_id=deck_id,
                user_id=user_id,
                source="MANUAL",
                position=position,
                front=front,
                back="b",
                card_type="QUESTION",
                target_difficulty=target_difficulty,
                publication_state=publication_state,
                delete_batch_id=delete_batch_id,
                version=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            ReviewState(
                review_state_id=str(_uuid.uuid4()),
                card_id=card_id,
                state=state,
                stability=stability,
                difficulty=1.0,
                due=now,
                reps=1 if state != "NEW" else 0,
                lapses=0,
                updated_at=now,
            )
        )
        session.commit()
    engine.dispose()
    return card_id


def _card_ids(resp: httpx.Response) -> list[str]:
    assert resp.status_code == 200, resp.text
    return [item["card_id"] for item in resp.json()["items"]]


# ---------- 位置序与会话稳定随机 ----------


def test_free_browse_position_order(client: TestClient, tmp_path: Path) -> None:
    """order=position（默认）：按 position 稳定升序。"""
    user = _user(client)
    deck_id = _make_deck(client, user)
    ids = [
        _make_card(client, user, deck_id, "a"),
        _make_card(client, user, deck_id, "b"),
        _make_card(client, user, deck_id, "c"),
    ]
    resp = client.get(f"/decks/{deck_id}/cards", headers=user)
    assert _card_ids(resp) == ids  # 创建顺序即 position 升序


def test_free_browse_random_order_session_stable(client: TestClient, tmp_path: Path) -> None:
    """order=random：会话稳定（同用户同牌组重复请求同序），且确实打乱位置序。"""
    user = _user(client)
    deck_id = _make_deck(client, user)
    ids = [
        _make_card(client, user, deck_id, "a"),
        _make_card(client, user, deck_id, "b"),
        _make_card(client, user, deck_id, "c"),
        _make_card(client, user, deck_id, "d"),
        _make_card(client, user, deck_id, "e"),
    ]
    r1 = _card_ids(client.get(f"/decks/{deck_id}/cards?order=random", headers=user))
    r2 = _card_ids(client.get(f"/decks/{deck_id}/cards?order=random", headers=user))
    assert r1 == r2  # 服务端不每翻一张重新洗牌（会话内稳定）
    assert sorted(r1) == sorted(ids)
    assert r1 != ids  # 确定性伪随机 ≠ 位置序


# ---------- 内容难度筛选 ----------


def test_free_browse_content_difficulty_filters(client: TestClient, tmp_path: Path) -> None:
    """content_difficulty 四类筛选：BASIC/UNDERSTANDING/DEEP_QUESTION/UNLABELED。"""
    user = _user(client)
    user_id = _user_id(client, tmp_path)
    deck_id = _make_deck(client, user)
    basic = _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=1, target_difficulty="BASIC"
    )
    under = _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=2, target_difficulty="UNDERSTANDING"
    )
    deep = _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=3, target_difficulty="DEEP_QUESTION"
    )
    unlabeled = _seed_card_with_state(tmp_path, user_id=user_id, deck_id=deck_id, position=4)

    assert _card_ids(
        client.get(f"/decks/{deck_id}/cards?content_difficulty=BASIC", headers=user)
    ) == [basic]
    assert _card_ids(
        client.get(f"/decks/{deck_id}/cards?content_difficulty=UNDERSTANDING", headers=user)
    ) == [under]
    assert _card_ids(
        client.get(f"/decks/{deck_id}/cards?content_difficulty=DEEP_QUESTION", headers=user)
    ) == [deep]
    assert _card_ids(
        client.get(f"/decks/{deck_id}/cards?content_difficulty=UNLABELED", headers=user)
    ) == [unlabeled]
    assert len(_card_ids(client.get(f"/decks/{deck_id}/cards", headers=user))) == 4


# ---------- 掌握筛选 ----------


def test_free_browse_mastery_filters(client: TestClient, tmp_path: Path) -> None:
    """mastery=mastered：state==REVIEW 且 stability>=21（契约 5.3）；unmastered 为其余。"""
    user = _user(client)
    user_id = _user_id(client, tmp_path)
    deck_id = _make_deck(client, user)
    mastered = _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=1, state="REVIEW", stability=25.0
    )
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=2, state="REVIEW", stability=10.0
    )  # REVIEW 但未达 21 天
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=3, state="LEARNING", stability=1.0
    )
    _seed_card_with_state(tmp_path, user_id=user_id, deck_id=deck_id, position=4)  # NEW

    assert _card_ids(client.get(f"/decks/{deck_id}/cards?mastery=mastered", headers=user)) == [
        mastered
    ]
    unmastered = _card_ids(client.get(f"/decks/{deck_id}/cards?mastery=unmastered", headers=user))
    assert len(unmastered) == 3  # 除掌握卡外的全部
    assert mastered not in unmastered


def test_free_browse_empty_state_without_matching_cards(client: TestClient, tmp_path: Path) -> None:
    """无符合条件卡片时展示空态（V25-STUDY-FR-10），不回退为全部卡片。"""
    user = _user(client)
    user_id = _user_id(client, tmp_path)
    deck_id = _make_deck(client, user)
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=1, target_difficulty="BASIC"
    )
    resp = client.get(f"/decks/{deck_id}/cards?content_difficulty=DEEP_QUESTION", headers=user)
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ---------- 零副作用与可见性 ----------


def test_free_browse_zero_side_effects(client: TestClient, tmp_path: Path) -> None:
    """自由刷题不创建事件、不改变排程、不计今日完成数（V25-STUDY-FR-10/AC-04）。"""
    import sqlalchemy

    from infra.db.session import create_db_engine

    user = _user(client)
    user_id = _user_id(client, tmp_path)
    deck_id = _make_deck(client, user)
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=1, state="REVIEW", stability=10.0
    )
    _seed_card_with_state(tmp_path, user_id=user_id, deck_id=deck_id, position=2)

    for params in (
        "",
        "?order=random",
        "?content_difficulty=BASIC",
        "?content_difficulty=UNLABELED",
        "?mastery=mastered",
        "?mastery=unmastered",
    ):
        resp = client.get(f"/decks/{deck_id}/cards{params}", headers=user)
        assert resp.status_code == 200

    db_file = next(tmp_path.glob("*.db"))
    engine = create_db_engine(f"sqlite:///{db_file}")
    with engine.connect() as conn:
        event_count = conn.execute(sqlalchemy.text("SELECT count(*) FROM review_events")).scalar()
        state_rows = conn.execute(
            sqlalchemy.text(
                "SELECT review_state_id, state, stability, due, reps FROM review_states ORDER BY card_id"
            )
        ).all()
    engine.dispose()
    assert event_count == 0  # 未创建任何复习事件
    assert len(state_rows) == 2
    # 排程状态未被改写（与直种值一致）
    states = {(row[1], row[2], row[3], row[4]) for row in state_rows}
    assert ("REVIEW", 10.0, "2026-08-10T00:00:00.000Z", 1) in states
    assert ("NEW", 0.0, "2026-08-10T00:00:00.000Z", 0) in states
    # 今日完成数不变
    plan = client.get("/study/today", headers=user)
    assert plan.status_code == 200, plan.text
    assert plan.json()["today_completed_count"] == 0


def test_free_browse_excludes_deleted_and_staged(client: TestClient, tmp_path: Path) -> None:
    """统一可见谓词（3.9）：删除批次与 STAGED 卡不进入自由刷题。"""
    user = _user(client)
    user_id = _user_id(client, tmp_path)
    deck_id = _make_deck(client, user)
    visible = _seed_card_with_state(tmp_path, user_id=user_id, deck_id=deck_id, position=1)
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=2, delete_batch_id=str(uuid.uuid4())
    )
    _seed_card_with_state(
        tmp_path, user_id=user_id, deck_id=deck_id, position=3, publication_state="STAGED"
    )
    assert _card_ids(client.get(f"/decks/{deck_id}/cards", headers=user)) == [visible]
