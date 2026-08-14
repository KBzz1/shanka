"""SCORING 阶段测试（spec §5.4/§8/§9；Task 11）：输出校验 / 确定性抽样 / 合批回写守卫。

- 基座同 test_batches_unit.py / test_planning_executor.py：真实 SQLite 全表建库 +
  mock transport（brief 提及的 session/settings_override/mock_chat fixture 仓库不存在，
  按仓库约定用 session_factory 定式——adaptation 见任务报告）。
- 种子：RUNNING+SCORING 任务 + 页文本 + 生成单元 + 每单元一批 + 已生成卡
  （process_next_batch 走真实生成路径落卡）；评分 mock 从 <SCORING_INPUT> 提取
  generation_item_id 保证 ID 集合守恒。
- 回写守卫断言：Card 5 评分字段非 NULL、总分 = 代码计算四维和、版本漂移 → 整组
  FAILED（STALE_SCORING_INPUT 内部原因入日志、error_code 兜底 GENERATION_FAILED）
  且卡评分留 NULL、失败不重试不阻塞（任务仍 COMPLETED）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import (
    ApiKey,
    Base,
    Batch,
    Card,
    Chapter,
    KnowledgePoint,
    LlmCallAttempt,
    PdfFile,
    Task,
    TextChunk,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.generation.scoring_validator import validate_scores
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
    engine = create_db_engine(f"sqlite:///{tmp_path / 'scoring.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _page_content(page_number: int) -> str:
    return f"第{page_number}页内容" * 20


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


def _card_response(card_type: str) -> str:
    if card_type == "TRUE_FALSE":
        return json.dumps(
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
    return json.dumps(
        {
            "cards": [
                {"type": "QUESTION", "question": "什么是锚定卡？", "answer": "由规划锚定的卡。"}
            ]
        },
        ensure_ascii=False,
    )


def _seed_scoring_task(
    session: Session,
    *,
    user_id: str,
    difficulties: list[str] | None = None,
    card_type: str = "QUESTION",
    n_units: int = 1,
    settings: Settings = _SETTINGS,
    stage: str = "SCORING",
    generate: bool = True,
) -> str:
    """RUNNING+{stage} 任务 + 页文本 + 生成单元（锚定难度/卡型循环）+ 每单元一批 +
    （generate）真实生成路径落卡。返回 task_id。"""
    from services.decks.service import create_deck

    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                password_hash="x",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="p.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if session.scalar(select(ApiKey.user_id).where(ApiKey.user_id == user_id)) is None:
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
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
        user_id=user_id,
        file_id=pdf.file_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            quantity_tendency="COMPACT",
            difficulty_ratio=DifficultyRatio(basic=0.4, understanding=0.4, application=0.2),
        ),
        now=_NOW,
    )
    # P4-4：executor 密钥查找已切 user 域
    task.status = "RUNNING"
    task.stage = "GENERATING"
    task.updated_at = _NOW
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    diffs = difficulties or ["BASIC"]
    units = [
        KnowledgePoint(
            knowledge_point_id=str(uuid.uuid4()),
            task_id=task.task_id,
            chapter_id=ch.chapter_id,
            source_chunk_id=chunks[0].chunk_id,  # 兼容投影（spec §3.1）
            topic=f"学习目标{i + 1}",
            priority=i + 1,
            status="PENDING",
            target_difficulty=diffs[i % len(diffs)],
            card_type=card_type,
            source_chunk_ids=json.dumps([c.chunk_id for c in chunks], ensure_ascii=False),
        )
        for i in range(n_units)
    ]
    session.add_all(units)
    session.flush()
    plan_batches(session, task_id=task.task_id, generation_units=units, now=_NOW)
    session.info["settings"] = settings  # process_next_batch 消费（executor 注入同款）
    if generate:
        client = _client(lambda r: _ok(_card_response(card_type)))
        while process_next_batch(session, task_id=task.task_id, client=client) > 0:
            pass
    task.stage = stage
    task.updated_at = _NOW
    session.commit()
    return task.task_id


def _scoring_response_from_request(
    request: httpx.Request,
    *,
    evidence: int = 2,
    correctness: int = 3,
    difficulty: int = 2,
    learning: int = 2,
) -> str:
    """从 <SCORING_INPUT> 提取本组 items → 每个 item 恰好一个分数（ID 集合守恒）。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0])
    scores = [
        {
            "generation_item_id": item["generation_item_id"],
            "evidence_score": evidence,
            "correctness_score": correctness,
            "difficulty_score": difficulty,
            "learning_value_score": learning,
        }
        for item in payload["items"]
    ]
    return json.dumps({"scores": scores}, ensure_ascii=False)


# ---------- Step 1：评分输出校验（spec §5.4/§5.6：ID 守恒 + 派生总分） ----------


def test_validate_scores_exact_set() -> None:
    """ID 集合 == 请求集合：输出四维 + 代码计算总分（模型声称的 total 与计算值相等）。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 2,
                "correctness_score": 3,
                "difficulty_score": 2,
                "learning_value_score": 2,
                "rubric_total_score": 9,
            }
        ]
    }
    out = validate_scores(raw, requested_ids={"g1"})
    assert out["g1"]["rubric_total_score"] == 9
    assert out["g1"]["evidence_score"] == 2
    assert out["g1"]["learning_value_score"] == 2


def test_validate_scores_missing_id_fails() -> None:
    with pytest.raises(AppError):
        validate_scores({"scores": []}, requested_ids={"g1"})


def test_validate_scores_total_mismatch_fails() -> None:
    """模型声称 total=9 但四维和=4 → 拒绝（整次 FAILED，不落部分分数）。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 1,
                "correctness_score": 1,
                "difficulty_score": 1,
                "learning_value_score": 1,
                "rubric_total_score": 9,
            }
        ]
    }
    with pytest.raises(AppError):
        validate_scores(raw, requested_ids={"g1"})


def test_validate_scores_extra_id_fails() -> None:
    """越权 ID（未请求的 g2 出现在结果）→ 拒绝。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 1,
                "correctness_score": 1,
                "difficulty_score": 1,
                "learning_value_score": 1,
            },
            {
                "generation_item_id": "g2",
                "evidence_score": 1,
                "correctness_score": 1,
                "difficulty_score": 1,
                "learning_value_score": 1,
            },
        ]
    }
    with pytest.raises(AppError):
        validate_scores(raw, requested_ids={"g1"})


def test_validate_scores_duplicate_id_fails() -> None:
    """同 ID 输出两次（无重复要求）→ 拒绝。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 1,
                "correctness_score": 1,
                "difficulty_score": 1,
                "learning_value_score": 1,
            },
            {
                "generation_item_id": "g1",
                "evidence_score": 2,
                "correctness_score": 2,
                "difficulty_score": 2,
                "learning_value_score": 2,
            },
        ]
    }
    with pytest.raises(AppError):
        validate_scores(raw, requested_ids={"g1"})


def test_validate_scores_schema_violation_fails() -> None:
    """四维越界（4 > 3）→ schema v2 原子拒绝。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 4,
                "correctness_score": 1,
                "difficulty_score": 1,
                "learning_value_score": 1,
            }
        ]
    }
    with pytest.raises(AppError):
        validate_scores(raw, requested_ids={"g1"})


def test_validate_scores_no_claimed_total_computes_sum() -> None:
    """未携带 rubric_total_score（schema v2 正常输出）→ 输出计算总分。"""
    raw = {
        "scores": [
            {
                "generation_item_id": "g1",
                "evidence_score": 3,
                "correctness_score": 2,
                "difficulty_score": 0,
                "learning_value_score": 2,
            }
        ]
    }
    out = validate_scores(raw, requested_ids={"g1"})
    assert out["g1"]["rubric_total_score"] == 7


# ---------- Step 4：确定性抽样 / 合批 / 回写守卫（spec §8） ----------


def _task_with_cards(
    session: Session, *, task_id: str
) -> tuple[Task, list[Card], list[KnowledgePoint]]:
    task = session.get(Task, task_id)
    assert task is not None and task.deck_id is not None
    cards = list(session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all())
    units = list(
        session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    )
    return task, cards, units


def _scoring_attempts(session: Session, *, task_id: str) -> list[LlmCallAttempt]:
    return list(
        session.scalars(
            select(LlmCallAttempt)
            .where(LlmCallAttempt.task_id == task_id, LlmCallAttempt.stage == "SCORING")
            .order_by(LlmCallAttempt.created_at, LlmCallAttempt.call_id)
        ).all()
    )


def test_sampling_deterministic(session_factory: Callable[[], Session]) -> None:
    """30 单元（10 BASIC + 10 UNDERSTANDING + 10 APPLICATION）：两次 plan_scoring_groups
    全等；BASIC/UNDERSTANDING 合批、APPLICATION 逐单元；组数 ≤ max_scoring_calls_per_task。"""
    from services.generation.scoring import plan_scoring_groups

    user = _uuid()
    difficulties = ["BASIC"] * 10 + ["UNDERSTANDING"] * 10 + ["APPLICATION"] * 10
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=difficulties, n_units=30)
        task = session.get(Task, task_id)
        assert task is not None
        g1 = plan_scoring_groups(session, task=task, settings=_SETTINGS)
        g2 = plan_scoring_groups(session, task=task, settings=_SETTINGS)
    assert [g.operation_key for g in g1] == [g.operation_key for g in g2]  # 确定性
    assert len(g1) <= _SETTINGS.max_scoring_calls_per_task
    assert len(g1) == 12  # BASIC 1 组（10 卡 ≤ 12）+ UNDERSTANDING 1 组 + APPLICATION 10 组
    assert all(len(g.unit_ids) == 1 for g in g1 if "APPLICATION" in g.group_key)
    assert all(len(g.card_ids) <= _SETTINGS.scoring_max_cards_per_call for g in g1)
    assert len({g.operation_key for g in g1}) == len(g1)  # 组 key 唯一


def test_plan_groups_split_by_card_cap(session_factory: Callable[[], Session]) -> None:
    """BASIC 合批受 scoring_max_cards_per_call 限制再拆：10 卡 + 上限 4 → 3 组（4/4/2）。"""
    from services.generation.scoring import plan_scoring_groups

    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        scoring_max_cards_per_call=4,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id = _seed_scoring_task(
            session, user_id=user, difficulties=["BASIC"], n_units=10, settings=settings
        )
        task = session.get(Task, task_id)
        assert task is not None
        groups = plan_scoring_groups(session, task=task, settings=settings)
    assert [len(g.card_ids) for g in groups] == [4, 4, 2]
    assert len(groups) <= settings.max_scoring_calls_per_task


def test_plan_groups_cap_reduction_by_layer_quota(
    session_factory: Callable[[], Session],
) -> None:
    """组批后调用数 > max_scoring_calls_per_task → 按层配额哈希缩减（≤ 上限、确定性）。"""
    from services.generation.scoring import plan_scoring_groups

    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        max_scoring_calls_per_task=5,
        _env_file=None,  # type: ignore[call-arg]
    )
    difficulties = ["BASIC"] * 10 + ["UNDERSTANDING"] * 10 + ["APPLICATION"] * 10
    with session_factory() as session:
        task_id = _seed_scoring_task(
            session, user_id=user, difficulties=difficulties, n_units=30, settings=settings
        )
        task = session.get(Task, task_id)
        assert task is not None
        g1 = plan_scoring_groups(session, task=task, settings=settings)
        g2 = plan_scoring_groups(session, task=task, settings=settings)
    assert len(g1) <= settings.max_scoring_calls_per_task
    assert [g.operation_key for g in g1] == [g.operation_key for g in g2]


def test_scoring_writes_scores_and_completes(session_factory: Callable[[], Session]) -> None:
    """评分成功：Card 5 字段非 NULL（总分 = 代码计算 9）、任务 COMPLETED（stage=SCORING）、
    账本 stage=SCORING SUCCESS（scoring_output schema v2 / rubric v2；不写 normalized_result）。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok(_scoring_response_from_request(request))

        task, cards, _ = _task_with_cards(session, task_id=task_id)
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task, cards, units = _task_with_cards(session, task_id=task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
    assert calls == 1
    assert task.status == "COMPLETED"
    assert task.stage == "SCORING"
    assert task.ended_at is not None
    assert cards[0].evidence_score == 2
    assert cards[0].correctness_score == 3
    assert cards[0].difficulty_score == 2
    assert cards[0].learning_value_score == 2
    assert cards[0].rubric_total_score == 9  # 代码计算四维和
    assert len(units) == 1
    assert len(attempts) == 1
    assert attempts[0].status == "SUCCESS"
    assert attempts[0].operation_key.startswith("scoring:")
    assert attempts[0].prompt_name == "scoring"
    assert attempts[0].schema_name == "scoring_output"
    assert attempts[0].schema_version == "v2"
    assert attempts[0].rubric_version == "v2"
    assert attempts[0].normalized_result is None  # 红线 4：SCORING 不写 normalized_result
    assert batch.coverage_rate == 1.0  # 批次质量字段由评分回写期重写（apply_batch_quality）


def test_scoring_preserves_dedup_duplicate_rate(
    session_factory: Callable[[], Session],
) -> None:
    """review 1/5：dedup-hit 批次（生成期 duplicated=1 → duplicate_rate=1.0）经
    SCORING 回写后 duplicate_rate 不被清零（spec §7 观测字段保留）。"""
    from services.generation.batches import _stable_uuid
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"], generate=False)
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        # 预置同 seed 既有卡（模拟"恢复/重入边缘"dedup 命中：生成响应与既有卡同 seed）
        gen_item = _stable_uuid(
            f"gen|{task_id}|{batch.batch_index}|QUESTION|什么是锚定卡？|由规划锚定的卡。"
        )
        session.add(
            Card(
                card_id=_uuid(),
                deck_id=task.deck_id,
                user_id=user,
                source="GENERATED",
                position=1,
                front="什么是锚定卡？",
                back="由规划锚定的卡。",
                card_type="QUESTION",
                question="什么是锚定卡？",
                answer="由规划锚定的卡。",
                generation_item_id=gen_item,
                target_difficulty="BASIC",
                version="v1",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.commit()
        # 生成：seed 命中既有卡 → fresh=False → duplicated=1 → duplicate_rate=1.0
        client = _client(lambda r: _ok(_card_response("QUESTION")))
        assert process_next_batch(session, task_id=task_id, client=client) == 1
        session.commit()
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        assert batch.duplicate_rate == 1.0  # 前置条件：dedup 观测已记录
        # 评分回写不得清零该观测
        scoring_client = _client(lambda r: _ok(_scoring_response_from_request(r)))
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=scoring_client)
        session.commit()
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
    assert batch.duplicate_rate == 1.0  # dedup 观测保留（不被评分重写清零）
    assert batch.coverage_rate == 1.0


def test_scoring_version_drift_rejected(session_factory: Callable[[], Session]) -> None:
    """评分调用后、回写前改 Card.version（模拟用户编辑）→ 整组 finish_failed
    （STALE_SCORING_INPUT 内部原因入日志、error_code 兜底 GENERATION_FAILED）、
    卡评分保持 NULL、任务仍 COMPLETED（非阻塞）。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])

        def handler(request: httpx.Request) -> httpx.Response:
            # chat 进行中（事务外）从另一连接注入用户编辑（card.version 变更）
            body = json.loads(request.content)
            user = body["messages"][-1]["content"]
            payload = json.loads(
                user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0]
            )
            gen_id = payload["items"][0]["generation_item_id"]
            with session_factory() as other:
                card = other.scalars(select(Card).where(Card.generation_item_id == gen_id)).one()
                card.version = "v2"
                other.commit()
            return _ok(_scoring_response_from_request(request))

        task, cards, _ = _task_with_cards(session, task_id=task_id)
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
    assert task.status == "COMPLETED"  # 组失败不阻塞任务完成
    assert [a.status for a in attempts] == ["FAILED"]
    assert attempts[0].error_code == "GENERATION_FAILED"  # 兜底错误码（内部原因日志区分）
    assert cards[0].evidence_score is None  # 旧分数不写回
    assert cards[0].rubric_total_score is None


def test_scoring_failure_non_blocking(session_factory: Callable[[], Session]) -> None:
    """评分 chat 抛 RetryableUpstreamError → 不重试（attempt_count == 1）、卡保留
    （card_count 不变）、任务仍 COMPLETED、账本记 FAILED。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, json={"error": {"message": "upstream down"}})

        task, cards, _ = _task_with_cards(session, task_id=task_id)
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
    assert (
        calls == 2
    )  # 1 次逻辑尝试 × 2 次 HTTP（T17 起 adapter 内部重试 1 次）；不重试语义 = attempts == 1（账本逻辑层）
    assert task.status == "COMPLETED"  # 失败不阻塞任务完成
    assert len(cards) == 1  # 卡保留
    assert cards[0].rubric_total_score is None
    assert len(attempts) == 1 and attempts[0].status == "FAILED"
    assert attempts[0].error_code == "API_KEY_UNAVAILABLE"


def test_scoring_invalid_output_group_failed(session_factory: Callable[[], Session]) -> None:
    """评分输出非法（非 JSON）→ 整组 FAILED（不落部分分数）、任务仍 COMPLETED。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        run_scoring_stage(
            session, task=task, settings=_SETTINGS, client=_client(lambda r: _ok("no"))
        )
        session.commit()
    with session_factory() as session:
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
    assert task.status == "COMPLETED"
    assert [a.status for a in attempts] == ["FAILED"]
    assert attempts[0].error_code == "GENERATION_FAILED"
    assert cards[0].rubric_total_score is None


def test_scoring_stage_cancel_guarded(session_factory: Callable[[], Session]) -> None:
    """组处理前任务已 CANCELLED → run_scoring_stage 不发请求（调用计数 0）、
    不覆盖 CANCELLED。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok(_scoring_response_from_request(request))

        task = session.get(Task, task_id)
        assert task is not None
        task.status = "CANCELLED"
        session.commit()
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
    assert calls == 0
    assert task is not None
    assert task.status == "CANCELLED"  # 最终条件更新不覆盖 cancel


def test_scoring_cap_reached_still_completes(session_factory: Callable[[], Session]) -> None:
    """账本 SCORING 尝试数已达 max_scoring_calls_per_task（恢复/上限调整边缘）→ 剩余组
    跳过不发调用，任务仍走最终条件更新 COMPLETED（不悬挂 RUNNING+SCORING）。"""
    from services.generation.ledger import create_attempt
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        max_scoring_calls_per_task=2,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id = _seed_scoring_task(
            session, user_id=user, difficulties=["BASIC"], settings=settings
        )
        # 预置 2 个其他组的 SCORING 尝试（STARTED 遗留）——本组尚未尝试，但总账已达上限
        for i in (1, 2):
            create_attempt(
                session,
                user_id=user,
                scope_type="TASK",
                scope_id=task_id,
                task_id=task_id,
                stage="SCORING",
                operation_key=f"scoring:other:{i}",
                input_fingerprint="fp",
                attempt_no=1,
                model="m",
                prompt_name="scoring",
                prompt_version="v2",
                now=_NOW,
            )
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok(_scoring_response_from_request(request))

        task = session.get(Task, task_id)
        assert task is not None
        run_scoring_stage(session, task=task, settings=settings, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
    assert calls == 0  # 上限已占 → 不再付费调用
    assert task is not None and task.status == "COMPLETED"  # 不悬挂
    assert len(attempts) == 2  # 未新增尝试


def test_enter_scoring_stage_transitions(session_factory: Callable[[], Session]) -> None:
    """GENERATING → SCORING 条件更新：RUNNING+GENERATING → True；再次调用/取消后 → False。"""
    from services.generation.scoring import enter_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, stage="GENERATING", generate=False)
        assert enter_scoring_stage(session, task_id=task_id, settings=_SETTINGS) is True
        task = session.get(Task, task_id)
        assert task is not None and task.stage == "SCORING"
        session.commit()
        assert enter_scoring_stage(session, task_id=task_id, settings=_SETTINGS) is False
        task = session.get(Task, task_id)
        assert task is not None
        task.status = "CANCELLED"
        task.stage = "GENERATING"  # 取消后回退 stage（模拟极端交错）
        session.commit()
        assert enter_scoring_stage(session, task_id=task_id, settings=_SETTINGS) is False


def test_executor_runs_scoring_after_generation(session_factory: Callable[[], Session]) -> None:
    """executor 接线：批循环结束 → enter_scoring_stage → 评分回写 → COMPLETED。
    同一 client 先服务生成调用（GENERATOR_INPUT）再服务评分调用（SCORING_INPUT）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, stage="GENERATING", generate=False)
    with session_factory() as session:

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user = body["messages"][-1]["content"]
            if "<SCORING_INPUT>" in user:
                return _ok(_scoring_response_from_request(request))
            return _ok(_card_response("QUESTION"))

        n = process_running_tasks(
            session, settings=_SETTINGS, client_factory=lambda _k: _client(handler)
        )
        session.commit()
        task, cards, _ = _task_with_cards(session, task_id=task_id)
    assert n == 1
    assert task.status == "COMPLETED"
    assert task.stage == "SCORING"
    assert len(cards) == 1
    assert cards[0].rubric_total_score == 9  # 生成 + 评分全链路


def test_scan_takes_over_scoring_orphan(session_factory: Callable[[], Session]) -> None:
    """SCORING 孤儿接管：RUNNING+SCORING 心跳超时 + 遗留 STARTED → 接管（CAS 条件更新）
    + STARTED→UNKNOWN + run_scoring_stage（账本为已尝试游标：已尝试组跳过、未尝试组续跑）。"""
    from services.generation.ledger import create_attempt
    from services.generation.scoring import plan_scoring_groups

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(
            session, user_id=user, difficulties=["APPLICATION", "APPLICATION"], n_units=2
        )
        task = session.get(Task, task_id)
        assert task is not None
        groups = plan_scoring_groups(session, task=task, settings=_SETTINGS)
        assert len(groups) == 2  # APPLICATION 逐单元
        create_attempt(
            session,
            user_id=user,
            scope_type="TASK",
            scope_id=task_id,
            task_id=task_id,
            stage="SCORING",
            operation_key=groups[0].operation_key,
            input_fingerprint=groups[0].input_fingerprint,
            attempt_no=1,
            model="m",
            prompt_name="scoring",
            prompt_version="v2",
            now=_NOW,
        )
        task.updated_at = "2026-08-10T00:00:00.000Z"  # 心跳超时（孤儿）
        session.commit()
    with session_factory() as session:

        def handler(request: httpx.Request) -> httpx.Response:
            return _ok(_scoring_response_from_request(request))

        n = process_running_tasks(
            session, settings=_SETTINGS, client_factory=lambda _k: _client(handler)
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        attempts = _scoring_attempts(session, task_id=task_id)
    assert n == 1  # 接管一个 SCORING 孤儿
    assert task.status == "COMPLETED"
    # 已尝试组跳过（STARTED→UNKNOWN 计为已尝试游标）；未尝试组续跑 SUCCESS
    assert [a.status for a in attempts] == ["UNKNOWN", "SUCCESS"]
    scored = [c for c in cards if c.rubric_total_score is not None]
    assert len(scored) == 1  # 仅未尝试组写回评分


# ---------- T11 Minor：字符上限拆组 / 空组 / 调用中取消 ----------


def test_plan_groups_split_by_input_char_cap(session_factory: Callable[[], Session]) -> None:
    """scoring_max_input_chars 双限之一：BASIC 合批受输入字符上限再拆。6 单元同层
    （共享 2 页原文）：每组重建页开销后 2 卡 = 1135 字符 ≤ 1200、3 卡 = 1507 > 1200
    → 组大小 [2, 2, 2]（card 上限 12 未触达，仅字符限拆分——确定性）。"""
    from services.generation.scoring import plan_scoring_groups

    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        scoring_max_input_chars=1200,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id = _seed_scoring_task(
            session, user_id=user, difficulties=["BASIC"], n_units=6, settings=settings
        )
        task = session.get(Task, task_id)
        assert task is not None
        groups = plan_scoring_groups(session, task=task, settings=settings)
    assert [len(g.card_ids) for g in groups] == [2, 2, 2]
    assert all(len(g.card_ids) <= settings.scoring_max_cards_per_call for g in groups)
    assert len({g.operation_key for g in groups}) == len(groups)  # 组 key 唯一


def test_scoring_group_entries_vanished_fails_no_call(
    session_factory: Callable[[], Session],
) -> None:
    """组数据消失（并发删除）：plan 后清空批次 generated_item_ids → 组 entries 空 →
    STARTED+FAILED 同事务记账、0 次调用（恢复按账本跳过——spec §8 已尝试游标）。"""
    from infra.llm.prompts import asset_versions
    from services.generation.scoring import _run_scoring_group, plan_scoring_groups

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        task = session.get(Task, task_id)
        assert task is not None
        groups = plan_scoring_groups(session, task=task, settings=_SETTINGS)
        assert len(groups) == 1
        # 并发删除：批次 generated_item_ids 清空（等价卡已消失）→ 组数据失效
        batch = session.scalars(select(Batch).where(Batch.task_id == task_id)).one()
        batch.generated_item_ids = "[]"
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok(_scoring_response_from_request(request))

        _run_scoring_group(
            session,
            task,
            groups[0],
            settings=_SETTINGS,
            client=_client(handler),
            versions=asset_versions(),
        )
        session.commit()
    assert calls == 0  # 空组不发调用
    with session_factory() as session:
        attempts = _scoring_attempts(session, task_id=task_id)
    assert [a.status for a in attempts] == ["FAILED"]
    assert attempts[0].error_code == "GENERATION_FAILED"


def test_scoring_cancel_during_call_no_writeback(session_factory: Callable[[], Session]) -> None:
    """评分 chat 进行中（事务外）另一连接取消 → 回写守卫（status!=RUNNING）→ 卡评分
    留 NULL、账本 STARTED 保留（恢复转 UNKNOWN）、最终条件更新不覆盖 CANCELLED。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])

        def handler(request: httpx.Request) -> httpx.Response:
            # chat 进行中（事务外）注入取消（另一连接——cancel handler 同款写入）
            with session_factory() as other:
                task_row = other.get(Task, task_id)
                assert task_row is not None
                task_row.status = "CANCELLED"
                task_row.ended_at = _NOW
                task_row.updated_at = _NOW
                other.commit()
            return _ok(_scoring_response_from_request(request))

        task, cards, _ = _task_with_cards(session, task_id=task_id)
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        session.commit()
    with session_factory() as session:
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        attempts = _scoring_attempts(session, task_id=task_id)
    assert task.status == "CANCELLED"  # 不被最终条件更新覆盖
    assert cards[0].rubric_total_score is None  # 不写回（守卫）
    assert [a.status for a in attempts] == ["STARTED"]  # 留给恢复转 UNKNOWN（§9）


def test_scoring_legacy_task_no_user_fails_clean(session_factory: Callable[[], Session]) -> None:
    """T3 Minor ①：legacy 任务（user_id NULL 的历史行）→ run_scoring_stage 干净
    GENERATION_FAILED（不 500 兜底语义、不发 LLM 调用、不产生无主账本行）。"""
    from services.generation.scoring import run_scoring_stage

    user = _uuid()
    with session_factory() as session:
        task_id = _seed_scoring_task(session, user_id=user, difficulties=["BASIC"])
        task = session.get(Task, task_id)
        assert task is not None
        task.user_id = None  # 直插模拟 user_id 缺失的历史行（SQLAlchemy 允许，无需其他表）
        session.commit()
    with session_factory() as session:
        task, cards, _ = _task_with_cards(session, task_id=task_id)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise AssertionError("legacy 任务不得发 LLM 调用")

        with pytest.raises(AppError) as exc_info:
            run_scoring_stage(session, task=task, settings=_SETTINGS, client=_client(handler))
        attempts = _scoring_attempts(session, task_id=task_id)
    assert exc_info.value.code.value == "GENERATION_FAILED"
    assert calls == 0  # guard 在 create_attempt/chat 前
    assert attempts == []  # 无无主账本行
    assert len(cards) >= 1  # 种子卡保留（只读失败，不破坏已有数据）
