"""services.cards 单卡重写集成测试（V6）：原地替换/失败保留/ReviewState 重置/Rubric 记录。

种子写入真实加密 Key（rewrite_card 解密路径）；client_factory 注入 mock transport（不触网）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from infra.db.models import ApiKey, Base, Card, Device, ReviewState
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.cards.rewrite import rewrite_card
from services.decks.service import create_deck

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_DEVICE = "dev"
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
    """dev 设备的 GENERATED 卡 + 非初始 ReviewState + AVAILABLE Key（重写前状态）。"""
    session.add(Device(device_id=_DEVICE, created_at=_NOW))
    session.flush()
    session.add(
        ApiKey(
            device_id=_DEVICE,
            encrypted_key=encrypted_key,
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, device_id=_DEVICE, name="D", now=_NOW)
    session.flush()
    card = Card(
        card_id=_uuid(),
        deck_id=deck.deck_id,
        device_id=_DEVICE,
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


def _client_returning(content: str) -> tuple[DeepSeekClient, list[str]]:
    """mock transport client + 捕获的 prompt（断言 Prompt 组装：附加要求 + JSON Schema 拼接）。"""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body["messages"][0]["content"])
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


def _metric_value(name: str, fragments: list[str]) -> float:
    """Prometheus 文本中指定 name+label 片段的数值（label 顺序不敏感）；不存在返回 0。"""
    for line in generate_latest(REGISTRY).decode().splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        labels = line.split("{", 1)[1].split("}", 1)[0]
        if all(frag in labels for frag in fragments):
            return float(line.split()[-1])
    return 0.0


def test_rewrite_succeeds_in_place(session_factory: Callable[[], Session]) -> None:
    """成功替换：card_id/position/source/code 不变；内容/generation_item_id/version/updated_at
    更新；ReviewState 原子重置 NEW 初始值；Rubric 5 字段落卡非 None。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id, deck_id, created_at = seeded.card_id, seeded.deck_id, seeded.created_at
    client, captured = _client_returning(_rewrite_cards_json())
    with session_factory() as session:
        card = rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements="用更简洁的语言",
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
        session.commit()
        replaced_id = card.card_id
    assert replaced_id == card_id
    assert len(captured) == 1
    assert "附加要求：用更简洁的语言" in captured[0]
    assert "请严格按以下 JSON Schema 输出：" in captured[0]
    assert "旧正面" in captured[0] and "旧背面" in captured[0]
    with session_factory() as session:
        stored = session.get(Card, card_id)
        assert stored is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert stored.card_id == card_id
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
        assert stored.target_difficulty == "APPLICATION"  # 保留原值
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
        # Rubric 5 字段落卡非 None（AC-06：低分照常替换）
        assert stored.evidence_score is not None
        assert stored.correctness_score is not None
        assert stored.difficulty_score is not None
        assert stored.learning_value_score is not None
        assert stored.rubric_total_score is not None


def test_rewrite_schema_invalid_preserves_card(session_factory: Callable[[], Session]) -> None:
    """Schema 违约（缺 question/answer，front/back 派生缺失）：REWRITE_SCHEMA_INVALID 422，
    原卡全字段 + review_state 原值保留（不做任何写）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    client, _ = _client_returning(json.dumps({"cards": [{"type": "QUESTION"}]}))
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        rewrite_card(
            session,
            device_id=_DEVICE,
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
        rewrite_card(
            session,
            device_id=_DEVICE,
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


def test_rewrite_llm_error_preserves_card(session_factory: Callable[[], Session]) -> None:
    """LLM 调用异常（transport 网络错误 → adapter GENERATION_FAILED）：保留原卡，不做任何写。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable")

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
    assert excinfo.value.code is ErrorCode.GENERATION_FAILED
    with session_factory() as session:
        card = session.get(Card, card_id)
        assert card is not None
        rs = session.scalar(select(ReviewState).where(ReviewState.card_id == card_id))
        assert rs is not None
        assert card.front == "旧正面"
        assert card.generation_item_id == "gen-old-0000"
        assert card.version == "v3"
        assert card.updated_at == _NOW
        assert rs.state == "REVIEW"
        assert rs.stability == 0.5


def test_rewrite_no_api_key_422(session_factory: Callable[[], Session]) -> None:
    """api_keys 无 AVAILABLE 行 → API_KEY_NOT_SET（422，契约 ch7「未保存 Key」语义），且不构造 client。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
        session.execute(
            delete(ApiKey).where(ApiKey.device_id == _DEVICE)
        )  # 移除 Key 行 → 无 AVAILABLE
        session.commit()
    calls = 0

    def factory(_api_key: str) -> DeepSeekClient:
        nonlocal calls
        calls += 1
        raise AssertionError("无 Key 时不得构造 client")

    with session_factory() as session, pytest.raises(AppError) as excinfo:
        rewrite_card(
            session,
            device_id=_DEVICE,
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
        rewrite_card(
            session,
            device_id=_DEVICE,
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
        rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
        )
    assert excinfo.value.code is ErrorCode.API_KEY_UNAVAILABLE


def test_rewrite_cross_device_404(session_factory: Callable[[], Session]) -> None:
    """跨设备查卡 → CARD_NOT_FOUND（统一 404，不暴露存在性）。"""
    with session_factory() as session:
        seeded = _seed_card(session)
        card_id = seeded.card_id
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        rewrite_card(
            session,
            device_id="other",
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
        )
    assert excinfo.value.code is ErrorCode.CARD_NOT_FOUND


def test_rewrite_true_false_response_switches_type(
    session_factory: Callable[[], Session],
) -> None:
    """T3 审查 Minor 1：QUESTION→TRUE_FALSE 类型切换——question/answer 清 None、
    statement/answer_boolean(int)/explanation 填充、front/back 由 statement/explanation 派生、
    Rubric 评分正常（statement/explanation 入评）。"""
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
        rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
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
        # Rubric 评分正常（5 字段非 None 且总分 > 0）
        assert stored.evidence_score is not None
        assert stored.correctness_score is not None
        assert stored.difficulty_score is not None
        assert stored.learning_value_score is not None
        assert stored.rubric_total_score is not None
        assert stored.rubric_total_score > 0
        # ReviewState 同正常路径原子重置
        assert rs.state == "NEW"
        assert rs.difficulty == 1.0
        assert rs.reps == 0


def test_rewrite_multi_card_response_takes_first(
    session_factory: Callable[[], Session],
) -> None:
    """T3 审查 Minor 3：响应多卡取首张——{"cards": [卡A, 卡B]} → 替换内容为卡A（首张），
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
        rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
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
    """final review Important 1：rewrite 的 chat 调用上报 8.3 llm 指标——成功一次 →
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
        rewrite_card(
            session,
            device_id=_DEVICE,
            card_id=card_id,
            custom_requirements=None,
            now=_NEW_NOW,
            settings=_SETTINGS,
            client_factory=lambda _api_key: client,
        )
        session.commit()
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
