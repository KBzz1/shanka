"""两阶段 AI 卡牌重写集成测试（structure-contract 3.19/6.5；V25-DECK-FR-08/AC-04；NV-05 Task 9）。

覆盖（brief RED 清单逐条 + 契约扩展）：
- 预览创建成功：201 字段契约（base_card_version/status=PENDING/expires_at=now+24h）+
  原卡零改动（front/version/updated_at/ReviewState 均不动）+ 一次 chat；
- 预览创建失败：LLM 上游异常 → GENERATION_FAILED 且无预览行/原卡不动；
  Schema 违约 → 422 REWRITE_SCHEMA_INVALID 且无预览行；
- 来源不可用：非生成卡/来源任务已删/来源章节已删 → 409 CARD_REWRITE_UNAVAILABLE（不触网）；
- custom requirement：进 prompt（user 信封）并持久化、随响应返回；非字符串 → 400；
- apply 成功：原子替换（正文/generation_item_id/version 递增/ReviewState 重置）+ 预览 APPLIED，
  全程零 chat（应用不调用 LLM，不需要 Key）；
- 重复 apply（新幂等键）：409 CARD_VERSION_CONFLICT，原卡 = 首次结果，只产生一次有效替换；
- 并发直接编辑：预览基于 v3，PATCH 后 apply → 409 CAS，卡保持编辑内容、预览保持 PENDING；
- 已删除卡：删除批次内 apply → 404；批次撤销后 apply → 200（内容/版本未变，CAS 成立）；
  批次过期硬删后 → 404（预览行经 FK CASCADE 消失）；
- 跨用户：create/apply/cancel 他人卡/预览 → 404（不暴露存在性）；
- API-key 安全：无 Key → 422 API_KEY_NOT_SET（不构造 client）；加密损坏 → 502
  API_KEY_UNAVAILABLE；错误响应不含畸形 Key 材料（红线 4）；
- 过期：apply 过期预览 → 409（24h TTL）；随后 cancel 幂等 204 且落库 EXPIRED；
- 幂等：create/apply 同键同 body 重放 → 首次响应（一次 chat/一次替换）；
- cancel：204 → CANCELLED；重复 cancel 204；已应用预览 cancel 204 no-op；不存在/跨用户 404。

时钟：API 层 monkeypatch app.api.cards.SystemClock（F0 可控时钟）；服务层显式 now 参数。
种子写入真实加密 Key（解密路径）+ 完整来源链（pdf→chapter→task→card.source_task_id/
chapter_id——来源可用谓词依赖，3.19）。mock transport 只返回假数据（红线 4）。
"""

import base64
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, delete, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.main import create_app
from domain.rewrite_preview import REWRITE_PREVIEW_EXPIRY_HOURS
from infra.db.models import (
    ApiKey,
    Card,
    CardRewritePreview,
    Chapter,
    Deck,
    PdfFile,
    ReviewState,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory, format_utc
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.cards.rewrite import apply_rewrite_preview, cancel_rewrite_preview
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

T0 = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)
T0_STR = "2026-08-10T09:00:00.000Z"
T0_PLUS_24H = format_utc(T0 + timedelta(hours=REWRITE_PREVIEW_EXPIRY_HOURS))
assert T0_PLUS_24H == "2026-08-11T09:00:00.000Z"  # 24 小时 TTL 常量与实现统一

# 预览成功内容：QUESTION 卡不带 front/back（由 question/answer 派生——生成输出允许）
_REWRITTEN_JSON = json.dumps(
    {"cards": [{"type": "QUESTION", "question": "新问题？改进后", "answer": "新答案。更详细。"}]},
    ensure_ascii=False,
)
# Schema 违约内容：缺 question/answer（派生后 front/back 缺失）→ REWRITE_SCHEMA_INVALID
_SCHEMA_INVALID_JSON = json.dumps({"cards": [{"type": "QUESTION"}]}, ensure_ascii=False)


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
        api_key_encryption_key="aa" * 32,  # 与 _ENCRYPTED_TEST_KEY 同配置（预览创建解密路径）
    )
    return TestClient(create_app(settings))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "rewrite-preview.db"


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
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _uuid() -> str:
    return str(uuid.uuid4())


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert value is not None
    return str(value)


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _inject_factory(client: TestClient, factory: Callable[[str], DeepSeekClient]) -> None:
    """mock transport 注入 app.state.client_factory（端点 getattr 读取；mypy: TestClient.app 为 ASGIApp）。"""
    cast(FastAPI, client.app).state.client_factory = factory


def _scripted_factory(calls: dict[str, int], *, content: str) -> Callable[[str], DeepSeekClient]:
    """mock transport 工厂：每次 chat 返回同一内容，调用计数到 calls["n"]（重放判定观测）。"""

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


def _failing_factory() -> Callable[[str], DeepSeekClient]:
    """apply/cancel 不得调用 LLM：被调用即断言失败（零 chat 守卫）。"""

    def factory(_api_key: str) -> DeepSeekClient:
        raise AssertionError("apply/cancel 不得构造 LLM client")

    return factory


# ---------- 种子：完整来源链（pdf→chapter→task）+ 真实加密 Key ----------


def _seed_generated_card(
    db_path: Path,
    *,
    user_id: str,
    encrypted_key: str = _ENCRYPTED_TEST_KEY,
    with_source_task: bool = True,
    with_chapter: bool = True,
    front: str = "旧正面",
    version: str = "v3",
) -> tuple[str, str, str, str]:
    """user 域 GENERATED 卡 + 完整来源链 + AVAILABLE Key + REVIEW 态（重写前状态）。

    返回 (card_id, deck_id, task_id, chapter_id)——with_source_task/with_chapter 为
    False 时卡上不挂对应来源（FK SET NULL 的等价终态，构造「来源已删」变体）。
    """
    factory = _db_factory(db_path)
    with factory() as session:
        if session.get(User, user_id) is None:
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
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=encrypted_key,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=T0_STR,
            )
        )
        deck = Deck(
            deck_id=_uuid(),
            user_id=user_id,
            name="D",
            source="MANUAL",
            version=T0_STR,
            created_at=T0_STR,
            updated_at=T0_STR,
        )
        session.add(deck)
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            user_id=user_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=1,
            status="PARSED",
            created_at=T0_STR,
        )
        session.add(pdf)
        session.flush()
        ch = Chapter(
            chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2
        )
        session.add(ch)
        session.flush()
        task = Task(
            task_id=_uuid(),
            user_id=user_id,
            file_id=pdf.file_id,
            deck_id=deck.deck_id,
            status="COMPLETED",
            stage=None,
            selected_chapters=json.dumps(
                [{"chapter_id": ch.chapter_id, "start_page": 1, "end_page": 2}]
            ),
            generation_config="{}",
            generated_card_count=1,
            resumable=0,
            created_at=T0_STR,
            updated_at=T0_STR,
        )
        session.add(task)
        session.flush()
        # with_source_task/with_chapter=False 时卡上不挂来源（FK SET NULL 的等价终态）
        card = Card(
            card_id=_uuid(),
            deck_id=deck.deck_id,
            user_id=user_id,
            source="GENERATED",
            position=1,
            front=front,
            back="旧背面",
            code="A1",
            card_type="QUESTION",
            question="旧问题？",
            answer="旧答案",
            generation_item_id="gen-old-0000",
            source_task_id=task.task_id if with_source_task else None,
            chapter_id=ch.chapter_id if with_chapter else None,
            target_difficulty="DEEP_QUESTION",
            knowledge_point_ids='["kp-1"]',
            version=version,
            created_at=T0_STR,
            updated_at=T0_STR,
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
                last_review="2026-08-09T00:00:00.000Z",
                reps=5,
                lapses=2,
                last_rating="GOOD",
                updated_at=T0_STR,
            )
        )
        session.commit()
        return card.card_id, deck.deck_id, task.task_id, ch.chapter_id


def _seed_manual_card(client: TestClient, user: dict[str, str]) -> str:
    """API 手动建卡（MANUAL 来源——不可重写，来源不可用组）。"""
    deck = client.post("/decks", json={"name": "D"}, headers={**user, **_idem()})
    assert deck.status_code == 201
    resp = client.post(
        f"/decks/{deck.json()['deck_id']}/cards",
        json={"front": "f", "back": "b"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    return str(resp.json()["card_id"])


def _preview(
    client: TestClient,
    user: dict[str, str],
    card_id: str,
    *,
    custom_requirements: str | None = None,
    calls: dict[str, int] | None = None,
) -> dict[str, Any]:
    """创建预览（注入 scripted 工厂；断言 201 后返回响应体）。"""
    if calls is None:
        calls = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    payload = {}
    if custom_requirements is not None:
        payload["custom_requirements"] = custom_requirements
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews", json=payload, headers={**user, **_idem()}
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


def _apply(
    client: TestClient, user: dict[str, str], card_id: str, rewrite_id: str
) -> dict[str, Any]:
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    return cast(dict[str, Any], resp.json())


def _preview_row(db_path: Path, *, rewrite_id: str) -> CardRewritePreview | None:
    with _db_factory(db_path)() as session:
        return session.get(CardRewritePreview, rewrite_id)


# ---------- RED：预览创建 ----------


def test_preview_create_201_card_unchanged(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """预览创建成功：201 字段契约（rewrite_id/card_id/base_card_version=v3/front/back/
    card_type/target_difficulty/custom_requirements/status=PENDING/expires_at=now+24h/
    created_at）；原卡零改动（front/version/updated_at/ReviewState 不动）；一次 chat。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))

    resp = client.post(
        f"/cards/{card_id}/rewrite-previews",
        json={"custom_requirements": "更简洁"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["card_id"] == card_id
    assert body["base_card_version"] == "v3"  # CAS 锚点 = 创建时卡版本
    assert body["front"] == "新问题？改进后"
    assert body["back"] == "新答案。更详细。"
    assert body["card_type"] == "QUESTION"
    assert body["target_difficulty"] == "DEEP_QUESTION"  # 原卡难度保留
    assert body["custom_requirements"] == "更简洁"
    assert body["status"] == "PENDING"
    assert body["expires_at"] == T0_PLUS_24H  # 24 小时 TTL（实现常量统一）
    assert body["created_at"] == T0_STR
    assert calls["n"] == 1  # 一次 chat

    # 原卡零改动：正文/版本/时间戳/ReviewState 全部不动（两阶段核心语义）
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"
        assert card.back == "旧背面"
        assert card.version == "v3"
        assert card.updated_at == T0_STR
        assert card.generation_item_id == "gen-old-0000"
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.state == "REVIEW"  # 预览创建不重置排程
        assert rs.reps == 5 and rs.lapses == 2
        row = session.scalar(
            select(CardRewritePreview).where(CardRewritePreview.card_id == card_id)
        )
        assert row is not None
        assert row.status == "PENDING"
        assert row.base_card_version == "v3"
        assert row.custom_requirements == "更简洁"  # 不保存完整 Prompt，只存要求


def test_preview_create_llm_error_no_preview_card_unchanged(
    client: TestClient, db_path: Path
) -> None:
    """预览创建失败（LLM 上游异常）：500 GENERATION_FAILED——无预览行、原卡不动、
    错误响应不含 Key 材料（红线 4：任何 Key/原始异常文本不得进入响应）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)

    def factory(_api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("upstream unreachable")

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    _inject_factory(client, factory)
    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "GENERATION_FAILED"
    assert "sk-test" not in resp.text  # 明文 Key 不进响应
    with _db_factory(db_path)() as session:
        assert (
            session.scalar(select(CardRewritePreview).where(CardRewritePreview.card_id == card_id))
            is None
        )  # 无预览行
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡保留
        assert card.version == "v3"
        assert card.updated_at == T0_STR


def test_preview_create_schema_invalid_422_no_preview(client: TestClient, db_path: Path) -> None:
    """预览创建失败（Schema 违约）：422 REWRITE_SCHEMA_INVALID——无预览行、原卡不动
    （结果为空/格式校验失败均保留原卡，FR-08）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_SCHEMA_INVALID_JSON))

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "REWRITE_SCHEMA_INVALID"
    assert calls["n"] == 1  # 已 chat（违约在响应侧判定）
    with _db_factory(db_path)() as session:
        assert (
            session.scalar(select(CardRewritePreview).where(CardRewritePreview.card_id == card_id))
            is None
        )
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡保留
        assert card.version == "v3"
        assert card.updated_at == T0_STR


# ---------- RED：来源可用性（3.19：来源项目/PDF/章节/来源页仍存在的 GENERATED 卡） ----------


def test_preview_create_manual_card_409_unavailable(client: TestClient) -> None:
    """非生成卡（手动新增）：409 CARD_REWRITE_UNAVAILABLE，不展示入口且不触网
    （不得伪造来源完成重写，FR-08）。"""
    user = _user(client)
    card_id = _seed_manual_card(client, user)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_REWRITE_UNAVAILABLE"
    assert calls["n"] == 0  # 来源判定在 chat 之前


def test_preview_create_source_task_deleted_409_unavailable(
    client: TestClient, db_path: Path
) -> None:
    """来源任务已删（source_task_id SET NULL——删历史保留卡终态）：409
    CARD_REWRITE_UNAVAILABLE（来源查看不可再承诺 → 不可重写）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    # with_source_task=False：任务被删除后的等价终态（FK SET NULL 已发生）
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id, with_source_task=False)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_REWRITE_UNAVAILABLE"
    assert calls["n"] == 0


def test_preview_create_source_chapter_deleted_409_unavailable(
    client: TestClient, db_path: Path
) -> None:
    """来源章节已删（chapter_id SET NULL——PDF/项目删除后的保留卡终态）：409
    CARD_REWRITE_UNAVAILABLE（来源页随之不可承诺）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id, with_chapter=False)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_REWRITE_UNAVAILABLE"
    assert calls["n"] == 0


# ---------- RED：custom requirement ----------


def test_preview_custom_requirement_in_prompt_and_persisted(
    client: TestClient, db_path: Path
) -> None:
    """custom requirement：进 prompt（user 消息 <REWRITE_INPUT> 信封内，不落 system）、
    持久化于预览行、随响应返回（AC-04 自定义要求真实一致）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    captured: list[dict[str, Any]] = []

    def factory(_api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": _REWRITTEN_JSON}}], "model": "m"},
            )

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    _inject_factory(client, factory)
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews",
        json={"custom_requirements": "用更简洁的语言，保留术语"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    assert resp.json()["custom_requirements"] == "用更简洁的语言，保留术语"
    assert len(captured) == 1
    user_msg = captured[0]["messages"][1]["content"]
    assert '"custom_requirements":"用更简洁的语言，保留术语"' in str(user_msg)
    assert "旧正面" in str(user_msg)  # 原卡内容入信封
    with _db_factory(db_path)() as session:
        row = session.scalar(
            select(CardRewritePreview).where(CardRewritePreview.card_id == card_id)
        )
        assert row is not None
        assert row.custom_requirements == "用更简洁的语言，保留术语"


def test_preview_create_custom_requirements_non_string_400(client: TestClient) -> None:
    """custom_requirements 非字符串：400 VALIDATION_ERROR（内联 object 手动校验）。"""
    user = _user(client)
    card_id = _seed_manual_card(client, user)
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews",
        json={"custom_requirements": 123},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------- RED：apply（CAS 原子替换） ----------


def test_apply_success_atomic_replace(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """apply 成功：200 返回新卡——card_id/deck_id/position/source/code 保留，正文/
    generation_item_id/version(v3→v4)/updated_at 更新，ReviewState 原子重置 NEW 初始值，
    预览 APPLIED；全程零 chat（应用不调用 LLM、不依赖 Key）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])
    _inject_factory(client, _failing_factory())  # apply 不得触网

    freeze_clock.now = T0 + timedelta(minutes=5)
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["card_id"] == card_id
    assert body["front"] == "新问题？改进后"
    assert body["back"] == "新答案。更详细。"
    assert body["version"] == "v4"  # v3 → 递增
    assert body["generation_item_id"] != "gen-old-0000"  # 新版本新标识
    assert body["target_difficulty"] == "DEEP_QUESTION"  # 原卡难度保留
    assert body["updated_at"] == "2026-08-10T09:05:00.000Z"

    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.deck_id == body["deck_id"]
        assert card.position == 1
        assert card.source == "GENERATED"
        assert card.code == "A1"
        assert card.front == "新问题？改进后"
        assert card.question == "新问题？改进后"
        assert card.answer == "新答案。更详细。"
        assert card.generation_item_id != "gen-old-0000"
        assert card.version == "v4"
        assert card.created_at == T0_STR  # created_at 不变
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert rs.state == "NEW"  # 正文改变 → 排程重置（2.10 新建卡初始值）
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0
        assert rs.due == "2026-08-10T09:05:00.000Z"
        assert rs.reps == 0 and rs.lapses == 0
        assert rs.last_review is None and rs.last_rating is None
        row = session.get(CardRewritePreview, rewrite_id)
        assert row is not None
        assert row.status == "APPLIED"  # 预览终态
        assert row.updated_at == "2026-08-10T09:05:00.000Z"


def test_apply_duplicate_409_once(client: TestClient, db_path: Path) -> None:
    """重复 apply（新幂等键）：409 CARD_VERSION_CONFLICT——只产生一次有效替换
    （重复点击替换或网络重试只能产生一次有效替换，FR-08），原卡 = 首次结果。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])

    first = _apply(client, user, card_id, rewrite_id)
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},  # 新幂等键 → 真实重试，非快照重放
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_VERSION_CONFLICT"
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == first["front"]  # 卡保持首次替换结果，未被二次触碰
        assert card.version == "v4"  # 未再递增
        row = session.get(CardRewritePreview, rewrite_id)
        assert row is not None
        assert row.status == "APPLIED"


def test_apply_direct_edit_cas_conflict_409(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """并发直接编辑：预览基于 v3 创建后用户 PATCH 编辑（version 变 ISO 时间戳）→
    apply 409 CARD_VERSION_CONFLICT（CAS 失败原卡不变）——卡保持编辑内容、
    预览保持 PENDING 可取消；直接编辑不产生额外重置排程警告（编辑即立即生效）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])

    resp = client.patch(
        f"/cards/{card_id}",
        json={"front": "手动编辑后正面", "back": "手动编辑后背面"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == T0_STR  # update_card：version = now（ISO，非 v4）

    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_VERSION_CONFLICT"
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "手动编辑后正面"  # 原卡 = 编辑结果，重写未覆盖
        assert card.version == T0_STR
        row = session.get(CardRewritePreview, rewrite_id)
        assert row is not None
        assert row.status == "PENDING"  # 预览保留，仍可取消


def test_apply_deleted_card_404_then_undo_applies(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """已删除卡：预览创建后卡片进入删除批次 → apply 404 CARD_NOT_FOUND（不可见即不存在）；
    批次撤销后卡内容/版本未变 → CAS 成立 → apply 200；批次过期硬删后预览行经 FK
    CASCADE 消失 → apply 404（无孤儿预览）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])

    batch = client.delete(f"/cards/{card_id}", headers={**user, **_idem()}).json()
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"

    # 窗口内撤销 → 卡恢复（version 未变）→ apply 成功
    undo = client.post(
        f"/card-deletion-batches/{batch['delete_batch_id']}/undo", headers={**user, **_idem()}
    )
    assert undo.status_code == 200
    applied = _apply(client, user, card_id, rewrite_id)
    assert applied["front"] == "新问题？改进后"

    # 过期硬删路径：新建预览 → 卡过期批硬删 → 预览行级联删除 → apply 404
    preview2 = _preview(client, user, card_id)
    rewrite2 = str(preview2["rewrite_id"])
    client.delete(f"/cards/{card_id}", headers={**user, **_idem()})
    freeze_clock.now = T0 + timedelta(seconds=11)  # 超过 10 秒撤销窗口
    # 惰性 finalizer 的持久化路径：删除另一张卡（mark_card_deleted 提交时清扫过期批
    # ——GET 读路径不 commit，finalize 随事务回滚，D-19 语义由写路径补提交）
    other = _seed_manual_card(client, user)
    sweep = client.delete(f"/cards/{other}", headers={**user, **_idem()})
    assert sweep.status_code == 200  # 本次提交同时硬删过期批内卡 + FINALIZED
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite2}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert _preview_row(db_path, rewrite_id=rewrite2) is None  # 卡硬删 → FK CASCADE


def test_apply_missing_preview_404(client: TestClient, db_path: Path) -> None:
    """apply 不存在的预览：404 CARD_NOT_FOUND（跨用户/不存在统一 404，不暴露存在性）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{_uuid()}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"


def test_apply_expired_409_then_cancel_commits_expired(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """过期预览 apply：24h TTL 到达（expires_at == now 即过期）→ 409
    CARD_VERSION_CONFLICT 原卡不变；随后 cancel 幂等 204 且落库 EXPIRED
    （EXPIRED 状态经成功路径持久化）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])

    freeze_clock.now = T0 + timedelta(hours=REWRITE_PREVIEW_EXPIRY_HOURS)  # expires_at == now
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CARD_VERSION_CONFLICT"
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡不变
        assert card.version == "v3"

    # cancel 幂等 204；成功路径提交 EXPIRED 终态
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}", headers={**user, **_idem()}
    )
    assert resp.status_code == 204
    row = _preview_row(db_path, rewrite_id=rewrite_id)
    assert row is not None
    assert row.status == "EXPIRED"


# ---------- RED：cancel（可幂等） ----------


def test_cancel_204_idempotent_and_applied_noop(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """cancel：204 → CANCELLED；重复 cancel（新键）仍 204（可幂等）；已应用预览
    cancel 204 no-op（APPLIED 终态不回退）；不存在/跨用户 404。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)

    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}", headers={**user, **_idem()}
    )
    assert resp.status_code == 204
    row = _preview_row(db_path, rewrite_id=rewrite_id)
    assert row is not None and row.status == "CANCELLED"
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}", headers={**user, **_idem()}
    )
    assert resp.status_code == 204  # 重复取消幂等

    # 已应用预览 cancel → 204 no-op（卡不被回退，预览保持 APPLIED）
    preview2 = _preview(client, user, card_id)
    rewrite2 = str(preview2["rewrite_id"])
    applied = _apply(client, user, card_id, rewrite2)
    assert applied["version"] == "v4"
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{rewrite2}", headers={**user, **_idem()}
    )
    assert resp.status_code == 204
    row2 = _preview_row(db_path, rewrite_id=rewrite2)
    assert row2 is not None and row2.status == "APPLIED"

    # 不存在 → 404
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{_uuid()}", headers={**user, **_idem()}
    )
    assert resp.status_code == 404


# ---------- RED：跨用户 ----------


def test_cross_user_create_apply_cancel_404(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """跨用户访问：user2 对 user1 的卡 create/apply/cancel → 404 CARD_NOT_FOUND
    （归属校验先于 Key 解析与来源判定，不暴露存在性；无业务残留）。"""
    user1 = _user(client, "user1", "pass-1111")
    user2 = _user(client, "user2", "pass-2222")
    user1_id = _user_id(db_path, "user1")
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user1_id)
    preview = _preview(client, user1, card_id)
    rewrite_id = str(preview["rewrite_id"])

    # create（他人卡，即使有 Key 也不触网）
    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user2, **_idem()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"

    # apply / cancel（他人预览）
    resp = client.post(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply", headers={**user2, **_idem()}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CARD_NOT_FOUND"
    resp = client.delete(
        f"/cards/{card_id}/rewrite-previews/{rewrite_id}", headers={**user2, **_idem()}
    )
    assert resp.status_code == 404

    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None and card.version == "v3"  # 原卡无业务残留
        row = session.get(CardRewritePreview, rewrite_id)
        assert row is not None and row.status == "PENDING"  # 预览未被他人触碰


# ---------- RED：API-key 安全错误 ----------


def test_preview_create_no_api_key_422_no_client(client: TestClient, db_path: Path) -> None:
    """未保存 API Key → 422 API_KEY_NOT_SET（chat 0 不触网、无预览行、原卡保留）。
    Key 解析失败在 chat 之前。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    with _db_factory(db_path)() as session:
        session.execute(delete(ApiKey).where(ApiKey.user_id == user_id))
        session.commit()
    _inject_factory(client, _failing_factory())

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "API_KEY_NOT_SET"
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None and card.front == "旧正面" and card.version == "v3"
        assert (
            session.scalar(select(CardRewritePreview).where(CardRewritePreview.card_id == card_id))
            is None
        )


def test_preview_create_corrupt_key_502_no_key_material(client: TestClient, db_path: Path) -> None:
    """加密数据损坏（解密失败）→ 502 API_KEY_UNAVAILABLE；错误响应不含畸形 Key 材料
    （红线 4：llm 层异常统一脱敏为 API_KEY_*/GENERATION_FAILED 错误码）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    corrupt = base64.b64encode(b"\x00" * 12 + b"corrupted-ciphertext").decode("ascii")
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id, encrypted_key=corrupt)
    _inject_factory(client, _failing_factory())

    resp = client.post(f"/cards/{card_id}/rewrite-previews", json={}, headers={**user, **_idem()})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "API_KEY_UNAVAILABLE"
    assert "corrupted" not in resp.text  # 原始异常文本/Key 材料不进响应
    assert "sk-test" not in resp.text
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None and card.version == "v3"


# ---------- RED：幂等（create/apply 同键重放） ----------


def test_preview_create_idempotent_replay_single_chat(client: TestClient, db_path: Path) -> None:
    """create 幂等：同键同 body 第二次 → 201 且响应体与首次一致（重放不二次 chat，
    只落一行预览——重复提交真实且一致，AC-04）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    calls: dict[str, int] = {"n": 0}
    _inject_factory(client, _scripted_factory(calls, content=_REWRITTEN_JSON))
    headers = {**user, **_idem()}
    payload = {"custom_requirements": "更简洁"}

    first = client.post(f"/cards/{card_id}/rewrite-previews", json=payload, headers=headers)
    assert first.status_code == 201
    assert calls["n"] == 1
    replay = client.post(f"/cards/{card_id}/rewrite-previews", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first.json()  # 幂等快照重放
    assert calls["n"] == 1  # 不二次 chat
    with _db_factory(db_path)() as session:
        rows = session.scalars(
            select(CardRewritePreview).where(CardRewritePreview.card_id == card_id)
        ).all()
        assert len(rows) == 1  # 只落一行预览
        assert rows[0].status == "PENDING"


def test_apply_idempotent_replay_single_replace(
    client: TestClient, db_path: Path, freeze_clock: _MutableClock
) -> None:
    """apply 幂等：同键同 body 第二次 → 200 且响应体与首次一致，只产生一次替换
    （version 仍 v4 未再递增；网络重试不重复替换，FR-08）。"""
    user = _user(client)
    user_id = _user_id(db_path)
    card_id, _, _, _ = _seed_generated_card(db_path, user_id=user_id)
    preview = _preview(client, user, card_id)
    rewrite_id = str(preview["rewrite_id"])
    _inject_factory(client, _failing_factory())
    headers = {**user, **_idem()}

    first = client.post(f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply", headers=headers)
    assert first.status_code == 200
    replay = client.post(f"/cards/{card_id}/rewrite-previews/{rewrite_id}/apply", headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    with _db_factory(db_path)() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.version == "v4"  # 只递增一次
        assert card.updated_at == "2026-08-10T09:00:00.000Z"


# ---------- 服务层：CAS 细节与状态机（无 HTTP 时钟） ----------


def test_service_apply_expired_writes_status_then_raises(db_engine: Engine) -> None:
    """服务层过期 apply：expires_at <= now 时先写 EXPIRED 再抛 409（与删除批次惰性
    清理同款 write-then-raise）；错误路径回滚后状态可由后续成功路径（cancel）持久化。"""
    factory = create_session_factory(db_engine)
    user_id = str(uuid.uuid4())
    with factory() as session:
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
        deck = Deck(
            deck_id=_uuid(),
            user_id=user_id,
            name="D",
            source="MANUAL",
            version=T0_STR,
            created_at=T0_STR,
            updated_at=T0_STR,
        )
        session.add(deck)
        session.flush()
        card = Card(
            card_id=_uuid(),
            deck_id=deck.deck_id,
            user_id=user_id,
            source="MANUAL",
            position=1,
            front="旧正面",
            back="旧背面",
            card_type="QUESTION",
            version="v1",
            created_at=T0_STR,
            updated_at=T0_STR,
        )
        session.add(card)
        session.flush()
        preview = CardRewritePreview(
            rewrite_id=_uuid(),
            user_id=user_id,
            card_id=card.card_id,
            base_card_version="v1",
            preview=json.dumps(
                {
                    "type": "QUESTION",
                    "front": "新正面",
                    "back": "新背面",
                    "question": "新问题",
                    "answer": "新答案",
                    "target_difficulty": None,
                }
            ),
            custom_requirements=None,
            status="PENDING",
            expires_at=T0_STR,  # 与 now 相等 → 已过期
            created_at=T0_STR,
            updated_at=T0_STR,
        )
        session.add(preview)
        session.commit()
        card_id, rewrite_id = card.card_id, preview.rewrite_id
    with factory() as session, pytest.raises(AppError) as excinfo:
        apply_rewrite_preview(
            session, user_id=user_id, card_id=card_id, rewrite_id=rewrite_id, now=T0_STR
        )
    assert excinfo.value.code is ErrorCode.CARD_VERSION_CONFLICT
    with factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None and stored.front == "旧正面"  # 原卡不变
        assert session.get(CardRewritePreview, rewrite_id) is not None
        # cancel 幂等成功路径提交 EXPIRED
        cancel_rewrite_preview(
            session, user_id=user_id, card_id=card_id, rewrite_id=rewrite_id, now=T0_STR
        )
        session.commit()
        row = session.get(CardRewritePreview, rewrite_id)
        assert row is not None and row.status == "EXPIRED"
