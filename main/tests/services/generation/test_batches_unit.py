"""分批生成单元测试（spec §7/§9；Task 10）：批=单元、双维锚定、页文本输入、账本同事务。

- 基座同 test_ledger.py / test_planning_executor.py：真实 SQLite 全表建库 + mock
  transport（brief 提及的 session/settings_override/mock_chat fixture 仓库不存在，
  按仓库约定用 session_factory 定式——adaptation 见任务报告）。
- 种子：RUNNING+GENERATING 任务 + 页文本（text_chunks）+ 生成单元（锚定难度/卡型/
  来源页）+ 按单元建批（plan_batches 新签名 generation_units/now）。
- 账本断言：调用前 STARTED 已提交、终态与卡入库同事务、崩溃恢复 STARTED→UNKNOWN、
  尝试数（含 UNKNOWN）为重试预算权威。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import (
    ApiKey,
    Base,
    Batch,
    Card,
    Chapter,
    Device,
    KnowledgePoint,
    LlmCallAttempt,
    PdfFile,
    Task,
    TextChunk,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from infra.llm.prompts import load_asset
from services.generation.batches import plan_batches, process_next_batch
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import process_running_tasks
from services.tasks.service import create_task

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_NOW = "2026-08-12T00:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'batches_unit.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _page_content(page_number: int) -> str:
    """确定性页文本（含信封边界字符 < >，供注入转义断言；chunk_id 可复算）。"""
    return f"<第{page_number}页>内容" * 20


def _seed_unit_task(
    session: Session,
    *,
    device_id: str,
    difficulty: str = "BASIC",
    card_type: str = "QUESTION",
    custom_requirements: str | None = None,
    n_units: int = 1,
    plan: bool = True,
    settings: Settings = _SETTINGS,
) -> str:
    """RUNNING+GENERATING 任务 + 页文本 + 生成单元 + （plan）按单元建批。返回 task_id。"""
    from services.decks.service import create_deck

    if session.get(Device, device_id) is None:
        session.add(Device(device_id=device_id, created_at=_NOW))
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        device_id=device_id,
        filename="p.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, device_id=device_id, name="D", now=_NOW)
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if session.scalar(select(ApiKey).where(ApiKey.device_id == device_id)) is None:
        session.add(
            ApiKey(
                device_id=device_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": _page_content(pn)} for pn in (1, 2)],
        now=_NOW,
    )
    task = create_task(
        session,
        device_id=device_id,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            quantity_tendency="COMPACT",
            difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
            custom_requirements=custom_requirements,
        ),
        now=_NOW,
    )
    task.status = "RUNNING"
    task.stage = "GENERATING"
    task.updated_at = _NOW
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    units = [
        KnowledgePoint(
            knowledge_point_id=str(uuid.uuid4()),
            task_id=task.task_id,
            chapter_id=ch.chapter_id,
            source_chunk_id=chunks[0].chunk_id,  # 兼容投影（spec §3.1）
            topic=f"学习目标{i + 1}",
            priority=i + 1,
            status="PENDING",
            target_difficulty=difficulty,
            card_type=card_type,
            source_chunk_ids=json.dumps([c.chunk_id for c in chunks], ensure_ascii=False),
        )
        for i in range(n_units)
    ]
    session.add_all(units)
    session.flush()
    if plan:
        plan_batches(session, task_id=task.task_id, generation_units=units, now=_NOW)
    session.info["settings"] = settings  # process_next_batch 消费（executor 注入同款）
    session.commit()
    return task.task_id


def _ok(content: str) -> httpx.Response:
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


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> DeepSeekClient:
    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def _valid_question_card() -> str:
    return json.dumps(
        {
            "cards": [
                {"type": "QUESTION", "question": "什么是锚定卡？", "answer": "由规划锚定的卡。"}
            ]
        },
        ensure_ascii=False,
    )


# ---------- 建批：1 单元 = 1 批 ----------


def test_plan_batches_one_batch_per_unit(session_factory: Callable[[], Session]) -> None:
    """plan_batches：每单元一批、batch_index=priority 序 1..N、generation_unit_id 必填。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device, n_units=3, plan=False)
        units = session.scalars(
            select(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
            .order_by(KnowledgePoint.priority)
        ).all()
        plan_batches(session, task_id=task_id, generation_units=units, now=_NOW)
        session.commit()
    with session_factory() as session:
        rows = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
        task = session.get(Task, task_id)
    assert len(rows) == 3
    assert {b.generation_unit_id for b in rows} == {u.knowledge_point_id for u in units}
    assert [b.batch_index for b in rows] == [1, 2, 3]  # priority 序 1..N
    assert all(b.status == "PENDING" and b.retry_count == 0 for b in rows)
    assert task is not None
    assert task.total_batch_count == 3
    assert task.completed_batch_count == 0


# ---------- 锚定生成：合法卡入库 ----------


def test_process_batch_anchored_card(session_factory: Callable[[], Session]) -> None:
    """锚定 QUESTION+BASIC：合法卡入库——卡型=锚定、target_difficulty=锚定值、version=v1、
    front/back 确定性投影；评分 5 字段留 NULL 待 SCORING；Batch 兼容投影同一次调用结果。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(
            session, device_id=device, difficulty="BASIC", card_type="QUESTION"
        )
        assert (
            process_next_batch(
                session, task_id=task_id, client=_client(lambda r: _ok(_valid_question_card()))
            )
            == 1
        )
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        card = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).one()
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        unit = session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
        ).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert card.card_type == "QUESTION"
    assert card.target_difficulty == "BASIC"  # 服务端写规划锚定值（不要求模型回传）
    assert card.version == "v1"
    assert card.front == "什么是锚定卡？" and card.back == "由规划锚定的卡。"  # 确定性投影
    # 评分 5 字段留 NULL 待 SCORING（T11 回写）
    assert card.evidence_score is None
    assert card.correctness_score is None
    assert card.difficulty_score is None
    assert card.learning_value_score is None
    assert card.rubric_total_score is None
    assert batch.status == "SUCCEEDED"
    assert batch.coverage_rate == 1.0  # 0/1 语义
    assert batch.retry_count == 0
    assert batch.cache_hit_tokens == 2 and batch.cache_miss_tokens == 8 and batch.output_tokens == 5
    assert batch.model == "deepseek-v4-flash" and batch.http_status == 200
    assert batch.prompt_version == "v3" and batch.schema_version == "v2"
    assert unit.status == "PROCESSED"
    assert task.generated_card_count == 1
    assert task.completed_batch_count == 1
    # 账本同事务 SUCCESS（usage/资产版本按调用记录）
    assert len(attempts) == 1
    assert attempts[0].status == "SUCCESS"
    assert attempts[0].operation_key == f"generating:{batch.batch_id}"
    assert attempts[0].stage == "GENERATING"
    assert attempts[0].prompt_name == "generator"
    assert attempts[0].prompt_version == "v3"
    assert attempts[0].schema_name == "generator_output"
    assert attempts[0].schema_version == "v2"
    assert attempts[0].cache_hit == 2 and attempts[0].output_tokens == 5
    assert attempts[0].normalized_result is None  # 红线 4：GENERATING 不写 normalized_result


def test_process_batch_true_false_projection(session_factory: Callable[[], Session]) -> None:
    """锚定 TRUE_FALSE+APPLICATION：front=statement/back=explanation 投影 + answer_boolean 落库。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(
            session, device_id=device, difficulty="APPLICATION", card_type="TRUE_FALSE"
        )
        content = json.dumps(
            {
                "cards": [
                    {
                        "type": "TRUE_FALSE",
                        "statement": "该请求已完成认证因此可以执行。",
                        "answer_boolean": False,
                        "explanation": "认证只是必要条件之一。",
                    }
                ]
            },
            ensure_ascii=False,
        )
        assert (
            process_next_batch(session, task_id=task_id, client=_client(lambda r: _ok(content)))
            == 1
        )
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        card = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).one()
    assert card.card_type == "TRUE_FALSE"
    assert card.front == "该请求已完成认证因此可以执行。"
    assert card.back == "认证只是必要条件之一。"
    assert card.answer_boolean == 0
    assert card.target_difficulty == "APPLICATION"
    assert card.statement is not None and card.explanation is not None


# ---------- 输出校验层：锚定/数量/格式 ----------


def test_process_batch_wrong_type_rejected(session_factory: Callable[[], Session]) -> None:
    """卡型不符锚定（返回 TRUE_FALSE 但锚定 QUESTION）→ 0 合法卡 → FAILED 重试路径；
    重试成功 → SUCCEEDED（尝试数 = 账本权威，Batch.retry_count 兼容投影）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device, card_type="QUESTION")
        wrong = json.dumps(
            {
                "cards": [
                    {
                        "type": "TRUE_FALSE",
                        "statement": "陈述",
                        "answer_boolean": True,
                        "explanation": "解释",
                    }
                ]
            },
            ensure_ascii=False,
        )
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok(wrong if calls == 1 else _valid_question_card())

        client = _client(handler)
        assert process_next_batch(session, task_id=task_id, client=client) == 1
        session.commit()
    with session_factory() as session:
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert batch.status == "FAILED"  # 锚定不符 → 重试预算内
    assert batch.retry_count == 1  # 兼容投影：账本尝试数为权威
    assert len(attempts) == 1 and attempts[0].status == "FAILED"
    assert attempts[0].error_code == "GENERATION_FAILED"
    with session_factory() as session:
        session.info["settings"] = _SETTINGS  # executor 注入同款（process_next_batch 消费）
        assert process_next_batch(session, task_id=task_id, client=client) == 1  # 重试
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert batch.status == "SUCCEEDED"
    assert batch.retry_count == 1  # 尝试数 2 - 1 成功次 = 1 次失败投影
    assert [a.status for a in attempts] == ["FAILED", "SUCCESS"]
    assert len(cards) == 1 and cards[0].card_type == "QUESTION"


def test_process_batch_multi_card_rejected(session_factory: Callable[[], Session]) -> None:
    """多卡输出 → generator-output schema v2 maxItems=1 原子拒绝 → FAILED 重试路径。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device, card_type="QUESTION")
        multi = json.dumps(
            {
                "cards": [
                    {"type": "QUESTION", "question": "q1", "answer": "a1"},
                    {"type": "QUESTION", "question": "q2", "answer": "a2"},
                ]
            },
            ensure_ascii=False,
        )
        assert (
            process_next_batch(session, task_id=task_id, client=_client(lambda r: _ok(multi))) == 1
        )
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert cards == []  # 原始非法多项不得过滤/投影后被接受（spec §5.6）
    assert attempts[0].status == "FAILED"


def test_process_batch_invalid_json_rejected(session_factory: Callable[[], Session]) -> None:
    """非 JSON 响应 → 输出非法 → FAILED 重试路径（不 panic）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device)
        assert (
            process_next_batch(session, task_id=task_id, client=_client(lambda r: _ok("not json")))
            == 1
        )
        session.commit()
    with session_factory() as session:
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert batch.status == "FAILED"
    assert attempts[0].status == "FAILED"


def test_process_batch_empty_cards_source_insufficient(
    session_factory: Callable[[], Session],
) -> None:
    """合法显式空数组 = 安全弃权（§5.3）：单元 SKIPPED + SOURCE_INSUFFICIENT，不重试；
    账本记 SUCCESS（调用本身成功），覆盖=0。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device)
        assert (
            process_next_batch(
                session, task_id=task_id, client=_client(lambda r: _ok('{"cards": []}'))
            )
            == 1
        )
        session.commit()
        # 不重试：下一轮无待处理批次
        assert (
            process_next_batch(
                session, task_id=task_id, client=_client(lambda r: _ok('{"cards": []}'))
            )
            == 0
        )
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        unit = session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
        ).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert batch.status == "SKIPPED"
    assert batch.coverage_rate == 0.0
    assert batch.retry_count == 0
    assert unit.status == "SKIPPED"
    assert cards == []
    assert len(attempts) == 1 and attempts[0].status == "SUCCESS"  # 弃权不重试
    assert task.completed_batch_count == 1


def test_process_batch_budget_exhausted_skipped(session_factory: Callable[[], Session]) -> None:
    """非法输出 3 次尝试（generation_retry_limit=2）→ 预算耗尽 → 批次 SKIPPED；
    尝试数（账本）为预算权威，Batch.retry_count=3 兼容投影，无第二套预算。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device)
        bad = json.dumps(
            {"cards": [{"type": "QUESTION"}]}, ensure_ascii=False
        )  # 缺 question/answer
        client = _client(lambda r: _ok(bad))
        assert process_next_batch(session, task_id=task_id, client=client) == 1
        session.commit()
        assert process_next_batch(session, task_id=task_id, client=client) == 1
        session.commit()
        assert process_next_batch(session, task_id=task_id, client=client) == 1
        session.commit()
        assert process_next_batch(session, task_id=task_id, client=client) == 0  # 无待处理
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        unit = session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
        ).one()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert batch.status == "SKIPPED"
    assert batch.retry_count == 3  # 投影 = 账本尝试数
    assert unit.status == "SKIPPED"
    assert cards == []
    assert len(attempts) == 3 and all(a.status == "FAILED" for a in attempts)
    assert task.completed_batch_count == 1


# ---------- Prompt 组装（spec §5.7 Generator 行） ----------


def test_process_batch_prompt_shape_and_page_input(session_factory: Callable[[], Session]) -> None:
    """请求形状：稳定 system（generator v3 + generator-output schema v2 原文）+ 动态
    user（<GENERATOR_INPUT> 安全 JSON：学习目标/锚定/有序页文本/自定义要求）；max_tokens=768；
    原文中的信封边界字符转义（可逆）。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(
            session,
            device_id=device,
            card_type="QUESTION",
            custom_requirements="使用简洁中文",
        )
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return _ok(_valid_question_card())

        assert process_next_batch(session, task_id=task_id, client=_client(handler)) == 1
        session.commit()
    body = captured["body"]
    assert isinstance(body, dict)
    messages = body["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    # 稳定资产在前、逐字节原文（generator v3 + schema v2 原文）
    assert system.startswith(load_asset("prompts", "generator"))
    assert "<GENERATOR_OUTPUT_SCHEMA>" in system
    assert load_asset("schemas", "generator_output") in system
    user = messages[1]["content"]
    assert user.startswith("<GENERATOR_INPUT>") and user.endswith("</GENERATOR_INPUT>")
    # 原文含 < > → 已转义（可逆：json 解析还原原文）
    assert "\\u003c第1页\\u003e" in user
    payload = json.loads(user.split("<GENERATOR_INPUT>", 1)[1].split("</GENERATOR_INPUT>", 1)[0])
    assert payload["learning_objective"] == "学习目标1"
    assert payload["target_difficulty"] == "BASIC"
    assert payload["card_type"] == "QUESTION"
    assert payload["custom_requirements"] == "使用简洁中文"
    # 有序页文本（page_number 升序；关联元数据不进入模型输入）
    assert [p["page_number"] for p in payload["source_material"]] == [1, 2]
    assert payload["source_material"][0]["content"] == _page_content(1)
    assert "chunk_id" not in payload and "generation_unit_id" not in payload
    # 请求体固定形状（spec §5.7）
    assert body["model"] == _SETTINGS.deepseek_model
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 768


def test_process_batch_input_char_cap_truncates_pages(
    session_factory: Callable[[], Session],
) -> None:
    """页文本总量 ≤ generator_max_input_chars：超预算页按页序确定性截断（纵深防御）。"""
    device = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        generator_max_input_chars=200,  # 页 1（140 字符）可容纳，页 2 超预算跳过
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device, settings=settings)
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["user"] = json.loads(request.content)["messages"][1]["content"]
            return _ok(_valid_question_card())

        assert process_next_batch(session, task_id=task_id, client=_client(handler)) == 1
        session.commit()
    payload = json.loads(
        str(captured["user"]).split("<GENERATOR_INPUT>", 1)[1].split("</GENERATOR_INPUT>", 1)[0]
    )
    assert [p["page_number"] for p in payload["source_material"]] == [1]


# ---------- 账本同事务与崩溃恢复（spec §9 硬规则） ----------


def test_process_batch_started_committed_before_chat(
    session_factory: Callable[[], Session],
) -> None:
    """任何 chat 调用前必须先有已提交的 STARTED 行：chat 进行中（mock handler 内）从
    另一连接可见 STARTED 已提交 + 批次抢占 PROCESSING 已提交。"""
    device = _uuid()
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device)

        def handler(request: httpx.Request) -> httpx.Response:
            with session_factory() as check:
                attempts = check.scalars(
                    select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
                ).all()
                assert len(attempts) == 1 and attempts[0].status == "STARTED"
                batch = check.scalars(select(Batch).where(Batch.task_id == task_id)).one()
                assert batch.status == "PROCESSING"
            return _ok(_valid_question_card())

        assert process_next_batch(session, task_id=task_id, client=_client(handler)) == 1
        session.commit()


def test_batch_ledger_same_transaction_crash_recovery(
    session_factory: Callable[[], Session],
) -> None:
    """§9 硬规则：终态与卡入库同事务——提交失败时保留 STARTED（崩溃模拟 → rollback 后
    账本仍 STARTED、卡未入库、批次 PROCESSING）；恢复（遗留 STARTED→UNKNOWN +
    PROCESSING 复位 FAILED）后按账本预算继续（尝试 2 成功，预算计数含 UNKNOWN）。"""
    device = _uuid()
    # 阶段 1：成功 chat 后终态提交失败（模拟崩溃）→ 领域写入回滚、STARTED 保留
    with session_factory() as session:
        task_id = _seed_unit_task(session, device_id=device)
        assert (
            process_next_batch(
                session, task_id=task_id, client=_client(lambda r: _ok(_valid_question_card()))
            )
            == 1
        )
        session.rollback()  # 终态提交失败（等价进程崩溃——executor 未 commit）
    with session_factory() as session:
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        card_count = session.scalar(
            select(func.count()).select_from(Card).where(Card.deck_id != None)
        )
        task = session.get(Task, task_id)
        assert task is not None
    assert [a.status for a in attempts] == ["STARTED"]  # 提交失败保留 STARTED（§9）
    assert batch.status == "PROCESSING"  # 抢占已提交（调用前）
    assert batch.retry_count == 0
    assert card_count == 0  # 卡未入库（同事务回滚）
    # 阶段 2：executor 扫描恢复（心跳超时 → 遗留 STARTED→UNKNOWN + PROCESSING→FAILED）
    # → 重试（尝试 2，预算含 UNKNOWN）→ SUCCEEDED + 卡入库 + 任务 COMPLETED
    with session_factory() as session:
        n = process_running_tasks(
            session,
            settings=_SETTINGS,
            client_factory=lambda _api_key: _client(lambda r: _ok(_valid_question_card())),
        )
        session.commit()
        task = session.get(Task, task_id)
    with session_factory() as session:
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert batch.status == "SUCCEEDED"
    assert batch.retry_count == 1  # 尝试 2 - 1 成功次
    # 孤儿 STARTED → UNKNOWN（GENERATING 账本行；T11 起任务完成后经 SCORING 阶段，
    # 本测试 mock 只服务生成调用——SCORING 行单独观测，不混入生成预算口径）
    generating = [a for a in attempts if a.stage == "GENERATING"]
    assert [a.status for a in generating] == ["UNKNOWN", "SUCCESS"]
    assert len(cards) == 1 and cards[0].card_type == "QUESTION"
