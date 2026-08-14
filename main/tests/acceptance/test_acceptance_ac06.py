"""验收测试：AC-06 单卡重写（PRD 5.13；迁移 schema + HTTP + mock transport 注入）。

映射（AC-06 三条）：
AC-06-a 可重写 → 200：card_id 与请求一致、front 新值、version 递增（mock transport 返回新内容）
AC-06-b Schema 通过才替换 → Schema 违约响应（front/back 空串）→ 422 REWRITE_SCHEMA_INVALID，
        重查原卡全字段不变（HTTP 层 + DB 直读双断言）
AC-06-c 失败保留原卡 → 422 违约时原卡不动（b）+ 错误路径（404）幂等表无记录、无业务残留、
        下次同键重试重新执行（T3 审查 Minor 2 集成确认）
幂等接线（T1 模式）：同键同 body 重放（chat 计数 1、响应体与首次一致）；同键异 body →
409 IDEMPOTENCY_CONFLICT（冲突在业务前判定，不二次 chat）。

注入：重写是请求内同步调用，`client.app.state.client_factory = factory`（端点 getattr 读取，
生产缺省 None 构造真实 client）；mock transport 只返回假数据（红线 4）。
"""

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Card, IdempotencyKey, ReviewState, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_NOW = "2026-08-11T00:00:00.000Z"

# 重写成功内容：QUESTION 卡不带 front/back（由 question/answer 派生）
_REWRITTEN_JSON = json.dumps(
    {"cards": [{"type": "QUESTION", "question": "新问题？改进后", "answer": "新答案。更详细。"}]},
    ensure_ascii=False,
)
# Schema 违约内容：front/back 空串（card.schema.json minLength 1）→ REWRITE_SCHEMA_INVALID
_SCHEMA_INVALID_JSON = json.dumps(
    {"cards": [{"type": "QUESTION", "front": "", "back": ""}]}, ensure_ascii=False
)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Settings]]:
    """迁移后 schema 的 TestClient（后台循环隔离：间隔 3600s）+ DB 路径 + 应用 settings。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac06.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶（连发 >5 req/s），显式调高隔离,
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环
        api_key_encryption_key="aa" * 32,  # 与 _ENCRYPTED_TEST_KEY 同配置（rewrite 解密路径）
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path, settings


def _uuid() -> str:
    return str(uuid.uuid4())


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _seed_card(
    db_path: Path, *, user_id: str, encrypted_key: str = _ENCRYPTED_TEST_KEY
) -> tuple[str, dict[str, object]]:
    """users + 牌组 + 真实加密 Key + GENERATED 卡（QUESTION, version v3）+ REVIEW 态（重写前状态）。

    返回 (card_id, before)：before 为原卡可迁移字段快照（「原卡全字段不变」断言基准）。
    encrypted_key 可注入畸形值（P4b 解密失败路径）。卡/牌组/Key 均 user 域（P4-4 起——
    ApiKey 用户域 Core 直写（只写所需列）。
    """
    factory = _db_factory(db_path)
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用
            session.add(
                User(
                    user_id=user_id,
                    username=f"u-{user_id[:8]}",
                    password_hash="x",
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=encrypted_key,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        session.flush()
        deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
        session.flush()
        card = Card(
            card_id=_uuid(),
            deck_id=deck.deck_id,
            user_id=user_id,
            source="GENERATED",
            position=1,
            front="旧正面",
            back="旧背面",
            code="A1",
            card_type="QUESTION",
            question="旧问题？",
            answer="旧答案",
            generation_item_id="gen-old-0000",
            target_difficulty="APPLICATION",
            knowledge_point_ids='["kp-1"]',
            evidence_score=1,
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
    before: dict[str, object] = {
        "front": "旧正面",
        "back": "旧背面",
        "code": "A1",
        "card_type": "QUESTION",
        "question": "旧问题？",
        "answer": "旧答案",
        "statement": None,
        "explanation": None,
        "answer_boolean": None,
        "generation_item_id": "gen-old-0000",
        "target_difficulty": "APPLICATION",
        "knowledge_point_ids": '["kp-1"]',
        "evidence_score": 1,
        "version": "v3",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return card.card_id, before


def _inject_factory(client: TestClient, factory: Callable[[str], DeepSeekClient]) -> None:
    """mock transport 注入 app.state.client_factory（端点 getattr 读取；mypy: TestClient.app 为 ASGIApp）。"""
    cast(FastAPI, client.app).state.client_factory = factory


def _scripted_factory(calls: dict[str, int], *, content: str) -> Callable[[str], DeepSeekClient]:
    """mock transport 工厂：每次 chat 返回同一内容，调用计数到 calls["n"]（重放/冲突判定观测）。"""

    def factory(_api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "model": "deepseek-v4-flash",
                },
            )

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    return factory


def _idem_record(db_path: Path, *, user_id: str, path: str, key: str) -> IdempotencyKey | None:
    with _db_factory(db_path)() as session:
        return session.scalar(
            select(IdempotencyKey).where(
                IdempotencyKey.user_id == user_id,
                IdempotencyKey.path == path,
                IdempotencyKey.idempotency_key == key,
            )
        )


def test_acceptance_ac06_rewrite_succeeds(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """AC-06-a 可重写：200，响应卡 card_id 与请求一致、front 新值、version 递增（一次 chat）。"""
    client, db_path, _ = ctx
    user = _user(client)
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))

    resp = client.post(
        f"/cards/{card_id}/rewrite",
        json={"custom_requirements": "更简洁"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["card_id"] == card_id  # 与请求一致
    assert body["front"] == "新问题？改进后"  # front 新值（question/answer 派生）
    assert body["back"] == "新答案。更详细。"
    assert body["version"] == "v4"  # v3 → 递增
    assert body["generation_item_id"] != "gen-old-0000"  # 新版本新标识
    assert calls["n"] == 1  # 一次 chat

    # DB 直读：原地替换落库
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "新问题？改进后"
        assert card.version == "v4"
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.state == "NEW"  # ReviewState 原子重置（2.10 新建卡初始值）
        assert rs.difficulty == 1.0


def test_acceptance_ac06_schema_invalid_preserves_card(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """AC-06-b 通过才替换：Schema 违约响应 → 422 REWRITE_SCHEMA_INVALID，原卡全字段不变。"""
    client, db_path, _ = ctx
    user = _user(client)
    card_id, before = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_SCHEMA_INVALID_JSON))
    headers = {**user, **_idem()}

    resp = client.post(f"/cards/{card_id}/rewrite", json={}, headers=headers)
    assert resp.status_code == 422  # HTTP 层：422
    assert resp.json()["error"]["code"] == "REWRITE_SCHEMA_INVALID"
    assert calls["n"] == 1  # 已 chat（Schema 违约在响应侧判定）

    # DB 直读：原卡全字段不变 + 幂等表无记录（非 2xx 不落）
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        for field, value in before.items():
            assert getattr(card, field) == value, f"{field} 不应被违约响应修改"
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.state == "REVIEW"  # ReviewState 原值保留
        assert rs.reps == 5 and rs.lapses == 2
    assert (
        _idem_record(
            db_path,
            user_id=_user_id(db_path),
            path=f"/cards/{card_id}/rewrite",
            key=headers["Idempotency-Key"],
        )
        is None
    )


def test_acceptance_ac06_idempotent_replay(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """幂等重放：同键同 body 第二次 → 200 且响应体与首次一致（重放不二次 chat，chat 计数 = 1）。"""
    client, db_path, _ = ctx
    user = _user(client)
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    headers = {**user, **_idem()}
    payload = {"custom_requirements": "更简洁"}

    first = client.post(f"/cards/{card_id}/rewrite", json=payload, headers=headers)
    assert first.status_code == 200
    assert calls["n"] == 1

    replay = client.post(f"/cards/{card_id}/rewrite", json=payload, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()  # 响应体与首次一致（幂等快照重放）
    assert calls["n"] == 1  # 重放不二次 chat

    # DB 直读：重放不产生第二次替换（version 仍 v4，未再递增）
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.version == "v4"


def test_acceptance_ac06_idempotency_conflict(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """幂等冲突：同键异 body → 409 IDEMPOTENCY_CONFLICT（冲突在业务前判定，不二次 chat）。"""
    client, db_path, _ = ctx
    user = _user(client)
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    headers = {**user, **_idem()}

    first = client.post(
        f"/cards/{card_id}/rewrite", json={"custom_requirements": "A"}, headers=headers
    )
    assert first.status_code == 200

    conflict = client.post(
        f"/cards/{card_id}/rewrite", json={"custom_requirements": "B"}, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert calls["n"] == 1  # 同键异 body 判定在 chat 之前，不二次调用


def test_acceptance_ac06_error_path_no_idempotency_record(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """错误路径（404）幂等表无记录 + 无业务残留：下次同键重试重新执行（T3 审查 Minor 2 集成确认）。"""
    client, db_path, _ = ctx
    user = _user(client)  # 注册 alice（_user_id 查询前提；重试用其 Bearer）
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    key = _idem()["Idempotency-Key"]

    # 跨用户查卡 → 404 CARD_NOT_FOUND（统一 404，不暴露存在性）
    other = _user(client, "user2", "pass-2222")
    resp = client.post(
        f"/cards/{card_id}/rewrite", json={}, headers={**other, "Idempotency-Key": key}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"
    assert calls["n"] == 0  # 查卡失败在 chat 之前

    # 幂等表无记录（404 非 2xx 不落）+ 原卡无业务残留（front/version/updated_at 不动）
    assert (
        _idem_record(
            db_path, user_id=_user_id(db_path, "user2"), path=f"/cards/{card_id}/rewrite", key=key
        )
        is None
    )
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"
        assert card.version == "v3"
        assert card.updated_at == _NOW

    # 同键重试（卡存在）→ 重新执行（chat 1 次、200）——未被 404 失败记录污染
    resp = client.post(
        f"/cards/{card_id}/rewrite", json={}, headers={**user, "Idempotency-Key": key}
    )
    assert resp.status_code == 200
    assert resp.json()["card_id"] == card_id
    assert resp.json()["front"] == "新问题？改进后"
    assert resp.json()["version"] == "v4"
    assert calls["n"] == 1


def test_acceptance_ac06_api_key_not_set_422(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """T4 审查 P4a：设备未保存 API Key → POST rewrite → 422 API_KEY_NOT_SET
    （chat 0 不触网、幂等表无记录、原卡保留）。"""
    client, db_path, _ = ctx
    user = _user(client)
    owner_id = _user_id(db_path)  # 会话外取 user_id：engine 级 BEGIN IMMEDIATE（读也写事务）
    card_id, _ = _seed_card(db_path, user_id=owner_id)
    with _db_factory(db_path)() as session:
        session.execute(delete(ApiKey).where(ApiKey.user_id == owner_id))
        session.commit()
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    headers = {**user, **_idem()}

    resp = client.post(f"/cards/{card_id}/rewrite", json={}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "API_KEY_NOT_SET"
    assert calls["n"] == 0  # Key 解析失败在 chat 之前，不触网
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡保留
        assert card.version == "v3"
    assert (
        _idem_record(
            db_path,
            user_id=_user_id(db_path),
            path=f"/cards/{card_id}/rewrite",
            key=headers["Idempotency-Key"],
        )
        is None
    )


def test_acceptance_ac06_corrupted_encrypted_key_502(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """T4 审查 P4b：api_keys 表加密数据损坏（畸形 encrypted_key 解密失败）→
    502 API_KEY_UNAVAILABLE（chat 0、幂等表无记录）。"""
    client, db_path, _ = ctx
    user = _user(client)
    corrupt = base64.b64encode(b"\x00" * 12 + b"corrupted-ciphertext").decode("ascii")
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path), encrypted_key=corrupt)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    headers = {**user, **_idem()}

    resp = client.post(f"/cards/{card_id}/rewrite", json={}, headers=headers)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "API_KEY_UNAVAILABLE"
    assert calls["n"] == 0  # 解密失败在 chat 之前，不触网
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡保留
        assert card.version == "v3"
    assert (
        _idem_record(
            db_path,
            user_id=_user_id(db_path),
            path=f"/cards/{card_id}/rewrite",
            key=headers["Idempotency-Key"],
        )
        is None
    )


def test_acceptance_ac06_cross_user_404(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """隔离（HTTP 层确认）：跨用户重写 → 404 CARD_NOT_FOUND（T3 已覆盖 service 层，
    此处断言 HTTP 错误响应 code；查卡失败在 chat 之前——不暴露存在性）。"""
    client, db_path, _ = ctx
    _user(client)  # 注册 alice（_user_id 查询前提）
    card_id, _ = _seed_card(db_path, user_id=_user_id(db_path))
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    other = _user(client, "user2", "pass-2222")

    resp = client.post(f"/cards/{card_id}/rewrite", json={}, headers={**other, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"
    assert calls["n"] == 0
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.version == "v3"  # 原卡无业务残留
