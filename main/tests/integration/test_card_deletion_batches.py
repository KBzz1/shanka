"""卡片删除批次集成测试（structure-contract 3.18/6.5；V25-DECK-FR-04、D-19/D-24）。

覆盖：单删可见性标记（不硬删）、连续删除合并整批重计时、pending 重启恢复、窗口内
（9.999s）撤销完整恢复、过期撤销 409、惰性 finalizer 幂等重跑、双设备合并/幂等重放、
过期批追加自动新建批、任务删除交集裁决（Task 6 语义：delete_generated_cards 对批次
内卡的硬删/解绑优先于撤销窗口，批次行保留）。

时钟注入（F0：服务代码只能经 infra.clock 取时间）：服务层函数显式 now 参数
（与 create_card/update_card 同款）；API 层 monkeypatch app.api.cards.SystemClock
为可控工厂（test_planning_executor 同款替换法）。
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.main import create_app
from infra.db.models import Card, CardDeletionBatch, Deck, ReviewState, Task, User
from infra.db.session import create_session_factory, format_utc
from services.cards.deletion import (
    finalize_expired_batches,
    mark_card_deleted,
    undo_deletion_batch,
)
from services.tasks.service import delete_task
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

T0 = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
T0_STR = "2026-08-10T09:00:00.000Z"


# ---------- API 层基座：迁移后 schema + 可控时钟 ----------


class _MutableClock:
    """可推进时钟：now_utc 返回当前值，测试直接赋值推进（F0 可控时钟）。"""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def _make_client(db_path: Path, storage_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=storage_path,
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶，显式调高
    )
    return TestClient(create_app(settings))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "deletion.db"


@pytest.fixture
def client(db_path: Path, tmp_path: Path) -> Iterator[TestClient]:
    with _make_client(db_path, tmp_path / "storage") as test_client:
        yield test_client


@pytest.fixture
def clock() -> _MutableClock:
    return _MutableClock(T0)


@pytest.fixture
def freeze_clock(monkeypatch: pytest.MonkeyPatch, clock: _MutableClock) -> _MutableClock:
    """API 层时钟冻结：替换 app.api.cards 模块的 SystemClock 为可控工厂。"""
    import app.api.cards as cards_api

    monkeypatch.setattr(cards_api, "SystemClock", lambda: clock)
    return clock


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _batch_id(batch: dict[str, object]) -> str:
    """服务层视图为 dict[str, object]，取 delete_batch_id 显式收窄为 str（mypy）。"""
    return str(batch["delete_batch_id"])


def _deck(client: TestClient, user: dict[str, str]) -> str:
    resp = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()})
    assert resp.status_code == 201
    return str(resp.json()["deck_id"])


def _card(client: TestClient, user: dict[str, str], deck_id: str, front: str = "f") -> str:
    resp = client.post(
        f"/decks/{deck_id}/cards", json={"front": front, "back": "b"}, headers={**user, **_idem()}
    )
    assert resp.status_code == 201
    return str(resp.json()["card_id"])


# ---------- 服务层种子 ----------


def _seed_user_deck(session: Session) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at=T0_STR,
            updated_at=T0_STR,
        )
    )
    session.flush()
    deck_id = str(uuid.uuid4())
    session.add(
        Deck(
            deck_id=deck_id,
            user_id=user_id,
            name="D",
            source="MANUAL",
            version=T0_STR,
            created_at=T0_STR,
            updated_at=T0_STR,
        )
    )
    session.flush()
    return user_id, deck_id


def _seed_card(
    session: Session,
    *,
    deck_id: str,
    user_id: str,
    position: int,
    source_task_id: str | None = None,
    front: str = "f",
) -> str:
    card_id = str(uuid.uuid4())
    session.add(
        Card(
            card_id=card_id,
            deck_id=deck_id,
            user_id=user_id,
            source="GENERATED" if source_task_id else "MANUAL",
            position=position,
            front=front,
            back="b",
            card_type="QUESTION",
            version=T0_STR,
            created_at=T0_STR,
            updated_at=T0_STR,
            source_task_id=source_task_id,
        )
    )
    session.add(
        ReviewState(
            review_state_id=str(uuid.uuid4()),
            card_id=card_id,
            state="NEW",
            stability=0.0,
            difficulty=1.0,  # ORM CHECK 1~10
            due=T0_STR,
            reps=0,
            lapses=0,
            updated_at=T0_STR,
        )
    )
    session.flush()
    return card_id


# ---------- API：单删 / 合并 / pending / 撤销 ----------


def test_card_deletion_batches_single_delete_marks_invisible(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """单卡删除 → 200 批次（不立即硬删，FR-04/D-19）：undo_until = now+10s；
    卡立即从列表与到期队列隐藏（统一可见谓词 3.9）。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_id = _card(client, user, deck_id)
    resp = client.delete(f"/cards/{card_id}", headers={**user, **_idem()})
    assert resp.status_code == 200, resp.text
    batch = resp.json()
    assert batch["card_ids"] == [card_id]
    assert batch["undo_until"] == "2026-08-10T09:00:10.000Z"
    assert batch["status"] == "PENDING"
    assert batch["created_at"] == batch["updated_at"] == T0_STR
    assert uuid.UUID(batch["delete_batch_id"])
    items = client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"]
    assert all(i["card_id"] != card_id for i in items)
    review = client.get(f"/decks/{deck_id}/review", headers=user).json()["items"]
    assert all(i["card_id"] != card_id for i in review)


def test_card_deletion_batches_consecutive_merge_refreshes_window(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """连续删除合并（D-24）：携带 delete_batch_id 追加同批，整批 undo_until 重计时 = now+10s。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_a = _card(client, user, deck_id, front="a")
    card_b = _card(client, user, deck_id, front="b")
    batch1 = client.delete(f"/cards/{card_a}", headers={**user, **_idem()}).json()
    freeze_clock.now = T0 + timedelta(seconds=5)
    resp = client.delete(
        f"/cards/{card_b}",
        params={"delete_batch_id": batch1["delete_batch_id"]},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    merged = resp.json()
    assert merged["delete_batch_id"] == batch1["delete_batch_id"]
    assert merged["card_ids"] == [card_a, card_b]  # 按加入顺序（pending_delete_at）
    assert merged["undo_until"] == "2026-08-10T09:00:15.000Z"  # 5s 追加 → 重计时 +10s
    assert merged["status"] == "PENDING"


def test_card_deletion_batches_pending_restart_recovery(
    client: TestClient, db_path: Path, tmp_path: Path
) -> None:
    """App 重启恢复（D-19，GET pending）：批持久化，重启后仍可检索并整批撤销。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_a = _card(client, user, deck_id, front="a")
    card_b = _card(client, user, deck_id, front="b")
    batch1 = client.delete(f"/cards/{card_a}", headers={**user, **_idem()}).json()
    batch2 = client.delete(
        f"/cards/{card_b}",
        params={"delete_batch_id": batch1["delete_batch_id"]},
        headers={**user, **_idem()},
    ).json()
    assert batch2["delete_batch_id"] == batch1["delete_batch_id"]
    resp = client.get("/card-deletion-batches/pending", headers=user)
    assert resp.status_code == 200
    pending = resp.json()["items"]
    assert len(pending) == 1
    assert pending[0]["delete_batch_id"] == batch1["delete_batch_id"]
    assert pending[0]["card_ids"] == [card_a, card_b]
    assert pending[0]["status"] == "PENDING"
    # 重启：同一 DB 新 client（新进程等价）
    client.close()
    with _make_client(db_path, tmp_path / "storage") as client2:
        user2 = _user(client2)  # 同库重新登录
        pending2 = client2.get("/card-deletion-batches/pending", headers=user2).json()["items"]
        assert [p["delete_batch_id"] for p in pending2] == [batch1["delete_batch_id"]]
        undo = client2.post(
            f"/card-deletion-batches/{batch1['delete_batch_id']}/undo",
            headers={**user2, **_idem()},
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["status"] == "UNDONE"
        assert undo.json()["card_ids"] == []
        items = client2.get(f"/decks/{deck_id}/cards", headers=user2).json()["items"]
        assert sorted(i["card_id"] for i in items) == sorted([card_a, card_b])


def test_card_deletion_batches_undo_within_window_restores_fully(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """窗口内（9.999s）撤销：整批恢复，内容/位置原样（FR-04）；同键重放同响应。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_id = _card(client, user, deck_id, front="q")
    batch = client.delete(f"/cards/{card_id}", headers={**user, **_idem()}).json()
    assert batch["undo_until"] == "2026-08-10T09:00:10.000Z"
    freeze_clock.now = T0 + timedelta(milliseconds=9999)  # 9.999s：仍 < undo_until
    key = _idem()
    undo = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**user, **key}
    )
    assert undo.status_code == 200, undo.text
    body = undo.json()
    assert body["status"] == "UNDONE"
    assert body["card_ids"] == []
    items = client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"]
    restored = [i for i in items if i["card_id"] == card_id]
    assert len(restored) == 1
    assert restored[0]["front"] == "q" and restored[0]["back"] == "b"
    assert restored[0]["position"] == 1
    assert restored[0]["delete_batch_id"] is None
    # 幂等重放：同键返回首次响应，不二次副作用
    replay = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**user, **key}
    )
    assert replay.status_code == 200
    assert replay.json() == body


def test_card_deletion_batches_undo_expired_409(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """过期撤销：undo_until 到达即 409 CARD_DELETE_WINDOW_EXPIRED（左闭右开窗口）；
    惰性 finalizer 硬删后 pending 为空，卡永久不可见（D-19，V2.5 无回收站）。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_id = _card(client, user, deck_id)
    batch = client.delete(f"/cards/{card_id}", headers={**user, **_idem()}).json()
    freeze_clock.now = T0 + timedelta(seconds=10)  # undo_until == now → 过期
    resp = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**user, **_idem()}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_DELETE_WINDOW_EXPIRED"
    assert client.get("/card-deletion-batches/pending", headers=user).json()["items"] == []
    items = client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"]
    assert all(i["card_id"] != card_id for i in items)
    resp = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**user, **_idem()}
    )
    assert resp.status_code == 409


def test_card_deletion_batches_undo_cross_user_404(client: TestClient) -> None:
    """跨用户撤销 → 404；pending 检索跨用户隔离（资源归属校验）。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_id = _card(client, user, deck_id)
    batch = client.delete(f"/cards/{card_id}", headers={**user, **_idem()}).json()
    other = _user(client, "user2", "pass-2222")
    resp = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**other, **_idem()}
    )
    assert resp.status_code == 404
    assert client.get("/card-deletion-batches/pending", headers=other).json()["items"] == []


def test_card_deletion_batches_two_device_merge_and_replay(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """双设备竞态：设备 2 追加到设备 1 的批 → 合并同一批、窗口重计时；同键重放单副作用。"""
    user = _user(client)  # 同一账号两设备（共享 user_id）
    deck_id = _deck(client, user)
    card_a = _card(client, user, deck_id, front="a")
    card_b = _card(client, user, deck_id, front="b")
    key1 = _idem()
    batch1 = client.delete(f"/cards/{card_a}", headers={**user, **key1}).json()
    freeze_clock.now = T0 + timedelta(seconds=3)
    batch2 = client.delete(
        f"/cards/{card_b}",
        params={"delete_batch_id": batch1["delete_batch_id"]},
        headers={**user, **_idem()},
    ).json()
    assert batch2["delete_batch_id"] == batch1["delete_batch_id"]
    assert batch2["card_ids"] == [card_a, card_b]
    assert batch2["undo_until"] == "2026-08-10T09:00:13.000Z"
    # 设备 1 同键重放：返回首次响应，不产生新副作用
    replay = client.delete(f"/cards/{card_a}", headers={**user, **key1})
    assert replay.status_code == 200
    assert replay.json() == batch1
    # 撤销恢复整批
    undo = client.post(
        f"/card-deletion-batches/{batch1['delete_batch_id']}/undo", headers={**user, **_idem()}
    )
    assert undo.status_code == 200
    items = client.get(f"/decks/{deck_id}/cards", headers=user).json()["items"]
    assert sorted(i["card_id"] for i in items) == sorted([card_a, card_b])


def test_card_deletion_batches_append_to_expired_batch_creates_new(
    client: TestClient, freeze_clock: _MutableClock
) -> None:
    """过期批的 delete_batch_id 不再可合并（3.18「仍为 PENDING」前置）：自动新建批。"""
    user = _user(client)
    deck_id = _deck(client, user)
    card_a = _card(client, user, deck_id, front="a")
    card_b = _card(client, user, deck_id, front="b")
    batch1 = client.delete(f"/cards/{card_a}", headers={**user, **_idem()}).json()
    freeze_clock.now = T0 + timedelta(seconds=10)
    resp = client.delete(
        f"/cards/{card_b}",
        params={"delete_batch_id": batch1["delete_batch_id"]},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    batch2 = resp.json()
    assert batch2["delete_batch_id"] != batch1["delete_batch_id"]
    assert batch2["card_ids"] == [card_b]
    assert batch2["status"] == "PENDING"


# ---------- 服务层：finalizer / 复习状态 / 任务删除交集 ----------


def test_card_deletion_batches_finalizer_idempotent_rerun(db_engine: Engine) -> None:
    """惰性 finalizer 幂等（3.18）：过期批硬删除全部卡并置 FINALIZED；重跑不再命中。"""
    factory = create_session_factory(db_engine)
    with factory() as session:
        user_id, deck_id = _seed_user_deck(session)
        card_a = _seed_card(session, deck_id=deck_id, user_id=user_id, position=1)
        card_b = _seed_card(session, deck_id=deck_id, user_id=user_id, position=2)
        session.commit()
    with factory() as session:
        batch1 = mark_card_deleted(
            session, user_id=user_id, card_id=card_a, delete_batch_id=None, now=T0_STR
        )
        batch2 = mark_card_deleted(
            session,
            user_id=user_id,
            card_id=card_b,
            delete_batch_id=_batch_id(batch1),
            now=T0_STR,
        )
        assert batch2["delete_batch_id"] == batch1["delete_batch_id"]
        session.commit()
    expired = format_utc(T0 + timedelta(seconds=10))
    with factory() as session:
        assert finalize_expired_batches(session, user_id=user_id, now=expired) == 1
        session.commit()
    with factory() as session:
        assert session.scalar(select(func.count(Card.card_id)).where(Card.user_id == user_id)) == 0
        batch = session.get(CardDeletionBatch, batch1["delete_batch_id"])
        assert batch is not None and batch.status == "FINALIZED"
        assert batch.updated_at == expired
        # 重跑：不再命中任何过期批（幂等）
        assert finalize_expired_batches(session, user_id=user_id, now=expired) == 0


def test_card_deletion_batches_undo_restores_review_state(db_engine: Engine) -> None:
    """撤销后复习状态完整恢复（FR-04）：删除只是可见性标记，ReviewState 行原样未动。"""
    factory = create_session_factory(db_engine)
    with factory() as session:
        user_id, deck_id = _seed_user_deck(session)
        card_id = _seed_card(session, deck_id=deck_id, user_id=user_id, position=1)
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        rs.state = "REVIEW"
        rs.stability = 7.5
        rs.difficulty = 4.0
        rs.due = "2026-08-11T00:00:00.000Z"
        rs.reps = 3
        rs.lapses = 1
        session.commit()
        snapshot = (rs.state, rs.stability, rs.difficulty, rs.due, rs.reps, rs.lapses)
    with factory() as session:
        batch = mark_card_deleted(
            session, user_id=user_id, card_id=card_id, delete_batch_id=None, now=T0_STR
        )
        assert batch["card_ids"] == [card_id]
        session.commit()
    with factory() as session:
        restored = undo_deletion_batch(
            session, user_id=user_id, delete_batch_id=_batch_id(batch), now=T0_STR
        )
        assert restored["status"] == "UNDONE"
        assert restored["card_ids"] == []
        session.commit()
    with factory() as session:
        card = session.scalar(select(Card).where(Card.card_id == card_id))
        assert card is not None and card.delete_batch_id is None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert (rs.state, rs.stability, rs.difficulty, rs.due, rs.reps, rs.lapses) == snapshot


def test_card_deletion_batches_task_delete_intersection_hard_delete(
    db_engine: Engine,
) -> None:
    """Task 6 裁决（delete_generated_cards=true）：任务删除硬删批次内卡——显式永久删除
    优先于撤销窗口；批次行保留（FINALIZED 前仍 PENDING），无可恢复卡。"""
    factory = create_session_factory(db_engine)
    with factory() as session:
        user_id, deck_id = _seed_user_deck(session)
        task_id = str(uuid.uuid4())
        session.add(
            Task(
                task_id=task_id,
                user_id=user_id,
                status="COMPLETED",
                selected_chapters="[]",
                generation_config="{}",
                generated_card_count=2,
                resumable=0,
                created_at=T0_STR,
                updated_at=T0_STR,
            )
        )
        session.flush()
        card_a = _seed_card(
            session,
            deck_id=deck_id,
            user_id=user_id,
            position=1,
            source_task_id=task_id,
            front="a",
        )
        _seed_card(
            session,
            deck_id=deck_id,
            user_id=user_id,
            position=2,
            source_task_id=task_id,
            front="b",
        )
        session.commit()
    with factory() as session:
        batch = mark_card_deleted(
            session, user_id=user_id, card_id=card_a, delete_batch_id=None, now=T0_STR
        )
        session.commit()
    with factory() as session:
        delete_task(session, user_id=user_id, task_id=task_id, delete_generated_cards=True)
        session.commit()
    with factory() as session:
        assert session.get(Card, card_a) is None  # 批次内卡被任务删除硬删
        assert session.get(Task, task_id) is None
        batch_row = session.get(CardDeletionBatch, _batch_id(batch))
        assert batch_row is not None and batch_row.status == "PENDING"  # 批次行保留
        restored = undo_deletion_batch(
            session, user_id=user_id, delete_batch_id=_batch_id(batch), now=T0_STR
        )
        assert restored["status"] == "UNDONE"
        assert restored["card_ids"] == []  # 已硬删，无卡可恢复
        session.commit()


def test_card_deletion_batches_task_delete_keep_cards_undo_restores(db_engine: Engine) -> None:
    """Task 6 裁决（delete_generated_cards=false）：批次内卡只解绑 source_task_id
    保留在批内，撤销后可见（无任务归属，与保留卡同语义）。"""
    factory = create_session_factory(db_engine)
    with factory() as session:
        user_id, deck_id = _seed_user_deck(session)
        task_id = str(uuid.uuid4())
        session.add(
            Task(
                task_id=task_id,
                user_id=user_id,
                status="COMPLETED",
                selected_chapters="[]",
                generation_config="{}",
                generated_card_count=1,
                resumable=0,
                created_at=T0_STR,
                updated_at=T0_STR,
            )
        )
        session.flush()
        card_a = _seed_card(
            session,
            deck_id=deck_id,
            user_id=user_id,
            position=1,
            source_task_id=task_id,
            front="a",
        )
        session.commit()
    with factory() as session:
        batch = mark_card_deleted(
            session, user_id=user_id, card_id=card_a, delete_batch_id=None, now=T0_STR
        )
        session.commit()
    with factory() as session:
        delete_task(session, user_id=user_id, task_id=task_id, delete_generated_cards=False)
        session.commit()
    with factory() as session:
        card = session.get(Card, card_a)
        assert card is not None
        assert card.delete_batch_id is not None  # 仍在批内（待撤销）
        assert card.source_task_id is None  # 已解绑保留
        restored = undo_deletion_batch(
            session, user_id=user_id, delete_batch_id=_batch_id(batch), now=T0_STR
        )
        assert restored["status"] == "UNDONE"
        assert restored["card_ids"] == []
        session.commit()
    with factory() as session:
        card = session.get(Card, card_a)
        assert card is not None and card.delete_batch_id is None
        assert card.source_task_id is None  # 撤销后可见且无任务归属
        assert card.publication_state == "PUBLISHED"


def test_card_deletion_batches_undo_expired_service_409(db_engine: Engine) -> None:
    """服务层过期撤销（无 API 层时钟）：undo_until == now → 409 CARD_DELETE_WINDOW_EXPIRED。"""
    factory = create_session_factory(db_engine)
    with factory() as session:
        user_id, deck_id = _seed_user_deck(session)
        card_id = _seed_card(session, deck_id=deck_id, user_id=user_id, position=1)
        session.commit()
    with factory() as session:
        batch = mark_card_deleted(
            session, user_id=user_id, card_id=card_id, delete_batch_id=None, now=T0_STR
        )
        session.commit()
    expired = format_utc(T0 + timedelta(seconds=10))
    with factory() as session:
        with pytest.raises(AppError) as excinfo:
            undo_deletion_batch(
                session, user_id=user_id, delete_batch_id=_batch_id(batch), now=expired
            )
        assert excinfo.value.code is ErrorCode.CARD_DELETE_WINDOW_EXPIRED
