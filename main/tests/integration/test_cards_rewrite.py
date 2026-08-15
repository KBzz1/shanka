"""services.cards.rewrite 两阶段重写集成测试（V2.5 3.19/6.5）：LLM 侧行为与账本。

本文件聚焦创建预览的 LLM 编排细节（双消息组装、账本、指标、Key 错误、响应语义）与
apply 的替换语义（类型切换/多卡取首）；完整生命周期/CAS/幂等/过期见
test_card_rewrite_preview_apply.py。

种子写入真实加密 Key（create_rewrite_preview 解密路径，用户域 Core 直写）+ 完整来源链
（pdf→chapter→task→card.source_task_id/chapter_id——来源可用谓词依赖）；client_factory
注入 mock transport（不触网）。
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import (
    ApiKey,
    Base,
    Card,
    CardRewritePreview,
    Chapter,
    LlmCallAttempt,
    PdfFile,
    ReviewState,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import load_asset
from services.cards.rewrite import apply_rewrite_preview, create_rewrite_preview
from services.decks.service import create_deck

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_USER = "user-1"
_NOW = "2026-08-11T00:00:00.000Z"
_NEW_NOW = "2026-08-11T01:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'rewrite.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_card(session: Session, *, encrypted_key: str = _ENCRYPTED_TEST_KEY) -> Card:
    """user 域 GENERATED 卡 + 完整来源链 + 非初始 ReviewState + AVAILABLE Key。"""
    session.add(
        User(
            user_id=_USER,
            username="u-1",
            email="u-1@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    session.execute(
        insert(ApiKey).values(
            user_id=_USER,
            encrypted_key=encrypted_key,
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, user_id=_USER, name="D", now=_NOW)
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=_USER,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    task = Task(
        task_id=_uuid(),
        user_id=_USER,
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
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(task)
    session.flush()
    card = Card(
        card_id=_uuid(),
        deck_id=deck.deck_id,
        user_id=_USER,
        source="GENERATED",
        position=1,
        front="旧正面",
        back="旧背面",
        code="A1",
        card_type="QUESTION",
        question="旧问题？",
        answer="旧答案",
        generation_item_id="gen-old-0000",
        source_task_id=task.task_id,
        chapter_id=ch.chapter_id,
        target_difficulty="DEEP_QUESTION",
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
    return card


def _rewrite_cards_json() -> str:
    """单张合法 QUESTION 卡（front/back 由 question/answer 派生——重写响应允许不带 front/back）。"""
    return json.dumps(
        {
            "cards": [
                {"type": "QUESTION", "question": "新问题？改进后", "answer": "新答案。更详细。"}
            ]
        },
        ensure_ascii=False,
    )


def _client_returning(content: str) -> tuple[DeepSeekClient, list[dict[str, Any]]]:
    """mock transport client + 捕获的请求体（断言双消息组装与 max_tokens）。"""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                },
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler)), captured


def _create_preview(
    session: Session,
    *,
    card_id: str,
    custom_requirements: str | None,
    client: DeepSeekClient,
    now: str = _NEW_NOW,
) -> CardRewritePreview:
    """创建预览并 commit（成功路径封装；返回预览行）。"""
    preview = create_rewrite_preview(
        session,
        user_id=_USER,
        card_id=card_id,
        custom_requirements=custom_requirements,
        now=now,
        settings=_SETTINGS,
        client_factory=lambda _api_key: client,
    )
    session.commit()
    return preview


def test_rewrite_preview_created_then_apply_replaces_in_place(
    session_factory: Callable[[], Session],
) -> None:
    """两阶段成功路径：预览创建（chat 1、原卡零改动）→ apply（零 chat）→ 原子替换：
    card_id/position/source/code 不变；内容/generation_item_id/version/updated_at 更新；
    ReviewState 原子重置 NEW 初始值；预览 APPLIED。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id, deck_id, created_at = seeded.card_id, seeded.deck_id, seeded.created_at
    client, captured = _client_returning(_rewrite_cards_json())
    with session_factory() as session:
        preview = _create_preview(
            session,
            card_id=card_id,
            custom_requirements="用更简洁的语言",
            client=client,
        )
        # 创建后原卡零改动（两阶段核心：预览不改卡）
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面" and card.back == "旧背面"
        assert card.version == "v3" and card.updated_at == _NOW
        assert preview.status == "PENDING"
        assert preview.base_card_version == "v3"
        assert preview.custom_requirements == "用更简洁的语言"
    assert len(captured) == 1  # 创建阶段一次 chat
    with session_factory() as session:
        replaced = apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_NEW_NOW
        )
        session.commit()
        replaced_id = replaced.card_id
    assert replaced_id == card_id
    assert len(captured) == 1  # apply 零 chat
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert stored.deck_id == deck_id
        assert stored.position == 1
        assert stored.source == "GENERATED"  # source 保留
        assert stored.code == "A1"  # code 不变
        assert stored.front == "新问题？改进后"
        assert stored.back == "新答案。更详细。"
        assert stored.card_type == "QUESTION"
        assert stored.question == "新问题？改进后"
        assert stored.answer == "新答案。更详细。"
        assert stored.generation_item_id != "gen-old-0000"  # 新版本新标识（旧标识随覆盖作废）
        assert stored.target_difficulty == "DEEP_QUESTION"  # 保留原值
        assert stored.knowledge_point_ids == '["kp-1"]'  # 保留原值
        assert stored.version == "v4"  # v3 → 递增
        assert stored.updated_at == _NEW_NOW
        assert stored.created_at == created_at  # created_at 不变
        # ReviewState 原子重置（2.10 新建卡初始值）
        assert rs.state == "NEW"
        assert rs.stability == 0.0
        assert rs.difficulty == 1.0
        assert rs.due == _NEW_NOW
        assert rs.reps == 0
        assert rs.lapses == 0
        assert rs.last_review is None
        assert rs.last_rating is None
        assert rs.updated_at == _NEW_NOW
        # T10 起 fake 评分退役：重写不写评分字段（原值保留，待 SCORING 回写；低分照常替换）
        assert stored.evidence_score == 1  # 种子原值保留
        assert stored.correctness_score is None
        assert stored.difficulty_score is None
        assert stored.learning_value_score is None
        assert stored.rubric_total_score is None
        row = session.get(CardRewritePreview, preview.rewrite_id)
        assert row is not None
        assert row.status == "APPLIED"


def test_rewrite_dual_message_shape_and_max_tokens(
    session_factory: Callable[[], Session],
) -> None:
    """§5.7 Rewrite 行：messages[0]=system（rewrite v4 + generator-output schema v3 原文，
    不含动态内容）、messages[1]=user（仅 <REWRITE_INPUT> 信封，不含资产）；max_tokens=768。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, captured = _client_returning(_rewrite_cards_json())
    with session_factory() as session:
        _create_preview(
            session,
            card_id=card_id,
            custom_requirements="用更简洁的语言",
            client=client,
        )
    assert len(captured) == 1
    body = captured[0]
    assert body["max_tokens"] == 768
    assert body["response_format"] == {"type": "json_object"}
    messages = body["messages"]
    assert len(messages) == 2
    system, user = messages[0], messages[1]
    assert system["role"] == "system" and user["role"] == "user"
    rewrite_asset = load_asset("prompts", "rewrite")
    schema_text = load_asset("schemas", "generator_output")
    assert rewrite_asset in system["content"]
    assert "<GENERATOR_OUTPUT_SCHEMA>" in system["content"]
    assert schema_text.strip() in system["content"]  # schema v3 原文在 system
    # system 只承载稳定资产：不含动态原卡/用户要求（资产文档对 <REWRITE_INPUT>
    # 协议的说明属稳定文本，非动态数据）
    assert "旧正面" not in system["content"]
    assert "用更简洁的语言" not in system["content"]
    # user 仅信封：不含资产与 schema
    assert user["content"].startswith("<REWRITE_INPUT>")
    assert user["content"].endswith("</REWRITE_INPUT>")
    assert "旧正面" in user["content"]
    assert '"custom_requirements":"用更简洁的语言"' in user["content"]
    assert rewrite_asset not in user["content"]
    assert "<GENERATOR_OUTPUT_SCHEMA>" not in user["content"]
    assert schema_text.strip() not in user["content"]


def test_rewrite_ledger_success_row(session_factory: Callable[[], Session]) -> None:
    """§9：REWRITE 账本——scope_type=CARD/scope_id=card_id/task_id 空；成功 SUCCESS +
    usage 三列 + 资产版本；normalized_result 不写；operation_key 含卡 ID/版本/幂等键 hash。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, _ = _client_returning(_rewrite_cards_json())
    idempotency_key = str(uuid.uuid4())
    with session_factory() as session:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            idempotency_key=idempotency_key,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
        session.commit()
    with session_factory() as session:
        rows = session.scalars(
            select(LlmCallAttempt).where(
                LlmCallAttempt.scope_type == "CARD",
                LlmCallAttempt.scope_id == card_id,
                LlmCallAttempt.stage == "REWRITE",
            )
        ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.user_id == _USER
        assert row.task_id is None  # 单卡重写无生成任务
        assert row.status == "SUCCESS"
        assert row.attempt_no == 1
        assert row.error_code is None
        assert row.operation_key == (
            f"rewrite:{card_id}:v3:{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        )
        assert row.prompt_name == "rewrite"
        assert row.prompt_version == "v4"
        assert row.schema_name == "generator_output"
        assert row.schema_version == "v3"
        assert row.cache_hit == 2 and row.cache_miss == 8 and row.output_tokens == 5
        assert row.http_status == 200
        assert row.normalized_result is None  # REWRITE 不写规范化结果（红线 4）
        # 指纹不含原文（红线 4）
        assert "旧正面" not in (row.input_fingerprint or "")
        assert "旧答案" not in (row.input_fingerprint or "")


def test_rewrite_ledger_failed_on_llm_error_no_preview(
    session_factory: Callable[[], Session],
) -> None:
    """§9：LLM 调用异常 → 账本 REWRITE FAILED（独立 commit 落库），无预览行，原卡保留。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable")

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED
    with session_factory() as session:
        row = session.scalar(
            select(LlmCallAttempt).where(
                LlmCallAttempt.scope_type == "CARD",
                LlmCallAttempt.scope_id == card_id,
                LlmCallAttempt.stage == "REWRITE",
            )
        )
        assert row is not None
        assert row.status == "FAILED"
        assert row.error_code == "GENERATION_FAILED"
        assert row.task_id is None
        assert (
            session.scalar(select(CardRewritePreview).where(CardRewritePreview.card_id == card_id))
            is None
        )  # 无预览行
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"  # 原卡保留
        assert card.version == "v3"


def test_rewrite_ledger_started_committed_before_chat(
    session_factory: Callable[[], Session],
) -> None:
    """§9 硬规则：chat 前 STARTED 已提交——transport 抛异常前用第二 session 观测到
    STARTED 行（模拟调用进行中进程崩溃后的观测）；异常后账本 FAILED 落库。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    observed: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        with session_factory() as observer:
            rows = observer.scalars(
                select(LlmCallAttempt).where(
                    LlmCallAttempt.scope_type == "CARD",
                    LlmCallAttempt.scope_id == card_id,
                    LlmCallAttempt.stage == "REWRITE",
                )
            ).all()
            observed.extend((r.status, r.attempt_no) for r in rows)
        raise httpx.ConnectError("upstream unreachable")

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session, pytest.raises(AppError):
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
    # 发调用前第二 session 已可见已提交 STARTED（adapter 内部 HTTP 重试会再次进入
    # transport——同一次逻辑调用内，观测到的仍是同一行 STARTED）
    assert observed and all(r == ("STARTED", 1) for r in observed)
    with session_factory() as session:
        row = session.scalar(
            select(LlmCallAttempt).where(
                LlmCallAttempt.scope_type == "CARD",
                LlmCallAttempt.scope_id == card_id,
                LlmCallAttempt.stage == "REWRITE",
            )
        )
        assert row is not None
        assert row.status == "FAILED"


def test_rewrite_schema_invalid_preserves_card(
    session_factory: Callable[[], Session],
) -> None:
    """Schema 违约（缺 question/answer，front/back 派生缺失）：REWRITE_SCHEMA_INVALID 422，
    无预览行，原卡全字段 + review_state 原值保留（不做任何写）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, _ = _client_returning(json.dumps({"cards": [{"type": "QUESTION"}]}))
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
    assert excinfo.value.code is ErrorCode.REWRITE_SCHEMA_INVALID
    with session_factory() as session:
        card = session.get(Card, card_id)
        assert card is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert (
            session.scalar(select(CardRewritePreview).where(CardRewritePreview.card_id == card_id))
            is None
        )
        assert card.front == "旧正面"
        assert card.back == "旧背面"
        assert card.card_type == "QUESTION"
        assert card.question == "旧问题？"
        assert card.answer == "旧答案"
        assert card.generation_item_id == "gen-old-0000"
        assert card.version == "v3"
        assert card.updated_at == _NOW
        assert rs.state == "REVIEW"
        assert rs.stability == 0.5
        assert rs.difficulty == 3.0
        assert rs.reps == 5
        assert rs.lapses == 2
        assert rs.last_review == "2026-08-10T00:00:00.000Z"
        assert rs.last_rating == "GOOD"


def test_rewrite_empty_cards_response_schema_invalid(
    session_factory: Callable[[], Session],
) -> None:
    """响应 JSON 合法但 cards 为空：同 Schema 违约路径（REWRITE_SCHEMA_INVALID，保留原卡）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, _ = _client_returning(json.dumps({"cards": []}))
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
    assert excinfo.value.code is ErrorCode.REWRITE_SCHEMA_INVALID
    with session_factory() as session:
        card = session.get(Card, card_id)
        assert card is not None
        assert card.front == "旧正面"
        assert card.version == "v3"
        assert card.updated_at == _NOW


def test_rewrite_no_api_key_422(session_factory: Callable[[], Session]) -> None:
    """api_keys 无 AVAILABLE 行 → API_KEY_NOT_SET（422，契约 ch7「未保存 Key」语义），且不构造 client。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
        session.execute(delete(ApiKey).where(ApiKey.user_id == _USER))  # 移除 Key 行 → 无 AVAILABLE
        session.commit()
    calls = 0

    def factory(_api_key: str) -> DeepSeekClient:
        nonlocal calls
        calls += 1
        raise AssertionError("无 Key 时不得构造 client")

    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=factory,
        )
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET
    assert calls == 0


def test_rewrite_no_encryption_config_422(session_factory: Callable[[], Session]) -> None:
    """Settings 未配置 api_key_encryption_key → key_from_settings None → API_KEY_NOT_SET（422）。"""
    with session_factory() as session:
        seeded = _seed_card(session)  # Key 行存在但加密配置缺失
        card_id = seeded.card_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=Settings(api_key_encryption_key=None),
        )
    assert excinfo.value.code is ErrorCode.API_KEY_NOT_SET


def test_rewrite_decrypt_failure_502(session_factory: Callable[[], Session]) -> None:
    """encrypted_key 与加密配置不符 → 解密失败 → API_KEY_UNAVAILABLE（502，与无 Key 422 区分）。"""
    other_key = key_from_settings(Settings(api_key_encryption_key="bb" * 32))
    assert other_key is not None
    wrong_payload = encrypt_key("sk-test-abc", other_key)
    with session_factory() as session:
        seeded = _seed_card(session, encrypted_key=wrong_payload)
        card_id = seeded.card_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id=_USER,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
        )
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_rewrite_cross_user_404(session_factory: Callable[[], Session]) -> None:
    """跨用户查卡 → CARD_NOT_FOUND（统一 404，不暴露存在性；归属校验先于来源判定）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        create_rewrite_preview(
            session,
            user_id="other",
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND


def test_rewrite_true_false_response_switches_type(
    session_factory: Callable[[], Session],
) -> None:
    """T3 审查 Minor 1：QUESTION→TRUE_FALSE 类型切换——apply 时 question/answer 清 None、
    statement/answer_boolean(int)/explanation 填充、front/back 由 statement/explanation 派生。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    response = json.dumps(
        {
            "cards": [
                {
                    "type": "TRUE_FALSE",
                    "statement": "珠穆朗玛峰是世界上最高的山峰。",
                    "answer_boolean": True,
                    "explanation": "珠穆朗玛峰海拔约 8848 米，超过地球上所有其他山峰，故判断正确。",
                }
            ]
        },
        ensure_ascii=False,
    )
    client, _ = _client_returning(response)
    with session_factory() as session:
        preview = _create_preview(session, card_id=card_id, custom_requirements=None, client=client)
        apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_NEW_NOW
        )
        session.commit()
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert stored.card_type == "TRUE_FALSE"
        assert stored.question is None  # 类型切换：旧类型字段清 None（不残留）
        assert stored.answer is None
        assert stored.statement == "珠穆朗玛峰是世界上最高的山峰。"
        assert stored.answer_boolean == 1  # 响应 JSON bool → 落库 int
        assert (
            stored.explanation == "珠穆朗玛峰海拔约 8848 米，超过地球上所有其他山峰，故判断正确。"
        )
        assert stored.front == stored.statement  # front/back 由 statement/explanation 派生
        assert stored.back == stored.explanation
        assert stored.version == "v4"
        # T10 起 fake 评分退役：重写不写评分字段（原值保留，待 SCORING 回写）
        assert stored.evidence_score == 1  # 种子原值保留
        assert stored.correctness_score is None
        assert stored.difficulty_score is None
        assert stored.learning_value_score is None
        assert stored.rubric_total_score is None
        # ReviewState 同正常路径原子重置
        assert rs.state == "NEW"
        assert rs.difficulty == 1.0
        assert rs.reps == 0


def test_rewrite_multi_card_response_takes_first(
    session_factory: Callable[[], Session],
) -> None:
    """T3 审查 Minor 3：响应多卡取首张——{"cards": [卡A, 卡B]} → 预览/替换内容为卡A（首张），
    卡B 被忽略（重写单卡语义）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    response = json.dumps(
        {
            "cards": [
                {"type": "QUESTION", "question": "首张问题", "answer": "首张答案"},
                {"type": "QUESTION", "question": "第二张问题", "answer": "第二张答案"},
            ]
        },
        ensure_ascii=False,
    )
    client, _ = _client_returning(response)
    with session_factory() as session:
        preview = _create_preview(session, card_id=card_id, custom_requirements=None, client=client)
        assert json.loads(preview.preview)["front"] == "首张问题"  # 预览内容 = 首张
        apply_rewrite_preview(
            session, user_id=_USER, card_id=card_id, rewrite_id=preview.rewrite_id, now=_NEW_NOW
        )
        session.commit()
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        assert stored.front == "首张问题"  # 内容 = 首张
        assert stored.back == "首张答案"
        assert stored.question == "首张问题"
        assert stored.answer == "首张答案"
        assert stored.front != "第二张问题"  # 卡B 被忽略
        assert stored.generation_item_id != "gen-old-0000"
        assert stored.version == "v4"


def test_rewrite_next_version_rule() -> None:
    r"""_next_version：^v(\d+)$ → 数字+1；其余（V1 手动卡 ISO 时间戳）→ v2。"""
    from services.cards.rewrite import _next_version

    assert _next_version("v1") == "v2"
    assert _next_version("v3") == "v4"
    assert _next_version("v9") == "v10"
    assert _next_version("v0") == "v1"
    assert _next_version("2026-08-11T00:00:00.000Z") == "v2"


def test_rewrite_reports_llm_metrics(session_factory: Callable[[], Session]) -> None:
    """final review Important 1：预览创建的 chat 调用上报 8.3 llm 指标——成功一次 →
    llm_requests_total{model="deepseek-v4-flash", http_status="200"} +1、
    llm_tokens_total 按 usage（cache_hit=2 + cache_miss=8 + output=5）。
    断言用 before/after 差值（REGISTRY 全局共享，批次路径可能已 inc 同 label）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, _ = _client_returning(_rewrite_cards_json())
    before_requests = _metric_value(
        "llm_requests_total", ['model="deepseek-v4-flash"', 'http_status="200"']
    )
    before_tokens = {
        kind: _metric_value("llm_tokens_total", [f'kind="{kind}"'])
        for kind in ("cache_hit", "cache_miss", "output")
    }
    with session_factory() as session:
        _create_preview(session, card_id=card_id, custom_requirements=None, client=client)
    after_requests = _metric_value(
        "llm_requests_total", ['model="deepseek-v4-flash"', 'http_status="200"']
    )
    after_tokens = {
        kind: _metric_value("llm_tokens_total", [f'kind="{kind}"'])
        for kind in ("cache_hit", "cache_miss", "output")
    }
    assert after_requests - before_requests == 1
    assert after_tokens["cache_hit"] - before_tokens["cache_hit"] == 2
    assert after_tokens["cache_miss"] - before_tokens["cache_miss"] == 8
    assert after_tokens["output"] - before_tokens["output"] == 5


def _metric_value(name: str, fragments: list[str]) -> float:
    """Prometheus 文本中指定 name+label 片段的数值（label 顺序不敏感）；不存在返回 0。"""
    for line in generate_latest(REGISTRY).decode().splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        labels = line.split("{", 1)[1].split("}", 1)[0]
        if all(frag in labels for frag in fragments):
            return float(line.split()[-1])
    return 0.0
