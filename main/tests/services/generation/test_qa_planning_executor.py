"""问答直通规划执行测试（V25-D-43）：提取 → 单元落库、空产出、失败三分支与 worker 分派。

基座同 test_planning_executor.py：真实 SQLite 全表建库 + mock transport client。mock chat
按 <QA_PLANNER_INPUT> 信封返回资料问答对（QUESTION / TRUE_FALSE 双形态）。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import (
    ApiKey,
    Base,
    Batch,
    Chapter,
    KnowledgePoint,
    LearningProject,
    LlmCallAttempt,
    Material,
    PdfFile,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.generation.planning_executor import claim_planning_task
from services.generation.qa_planning_executor import run_qa_planning
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import process_active_tasks
from services.tasks.service import create_task

_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_NOW = "2026-08-12T00:00:00.000Z"
_CLAIM_NOW = "2026-08-12T01:00:00.000Z"

_QA_PAIRS: list[dict[str, Any]] = [
    {
        "card_type": "QUESTION",
        "question": "什么是上下文窗口？",
        "answer": "模型一次推理能读取的最大文本长度。",
    },
    {
        "card_type": "TRUE_FALSE",
        "statement": "上下文窗口越长，模型单次能读取的文本越多。",
        "answer_boolean": True,
        "explanation": "资料未提供解析。",
    },
]


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'qa_planning.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_qa_task(
    session: Session,
    *,
    user_id: str,
    page_count: int = 2,
) -> tuple[str, str, str]:
    """GENERATING+PLANNING 的 QA_DIRECT 任务 + 章节 + 题库页文本；返回 (task_id, chapter_id, file_id)。"""
    from services.decks.service import create_deck

    session.add(
        User(
            user_id=user_id,
            username=f"u-{user_id[:8]}",
            email=f"u-{user_id[:8]}@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="bank.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="题库项目",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    session.add(
        Material(
            material_id=pdf.file_id,
            project_id=project.project_id,
            type="PDF",
            name="bank.pdf",
            status=None,
            created_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
    deck.project_id = project.project_id
    session.flush()
    ch = Chapter(
        chapter_id=_uuid(),
        file_id=pdf.file_id,
        material_id=pdf.file_id,
        name="第一章",
        source="MANUAL",
        start_page=1,
        end_page=page_count,
    )
    session.add(ch)
    session.flush()
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
        pages=[
            {
                "page_number": pn,
                "content": f"第{pn}页 1. 什么是上下文窗口？答：模型一次推理能读取的最大文本长度。",
            }
            for pn in range(1, page_count + 1)
        ],
        now=_NOW,
    )
    task = create_task(
        session,
        user_id=user_id,
        project_id=project.project_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            coverage_mode="BALANCED",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
            source_mode="QA_DIRECT",
        ),
        now=_NOW,
    )
    task.status = "GENERATING"
    task.stage = "PLANNING"
    task.updated_at = _NOW
    session.commit()
    return task.task_id, ch.chapter_id, pdf.file_id


def _ok_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 1,
            },
            "model": "deepseek-flash",
        },
    )


def _qa_handler(
    pairs_for: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    *,
    invalid: bool = False,
) -> Callable[[httpx.Request], httpx.Response]:
    """<QA_PLANNER_INPUT> mock：默认返回双形态问答对（引用首块）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        user = json.loads(request.content)["messages"][-1]["content"]
        assert "<QA_PLANNER_INPUT>" in user
        system = json.loads(request.content)["messages"][0]["content"]
        assert "题库整理员" in system  # qa-planner v1 资产加载进 system
        assert "<QA_PLANNER_OUTPUT_SCHEMA>" in system
        payload: dict[str, Any] = json.loads(
            user.split("<QA_PLANNER_INPUT>", 1)[1].split("</QA_PLANNER_INPUT>", 1)[0]
        )
        if invalid:
            return _ok_response("这不是 JSON")
        chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
        pairs = (
            pairs_for(payload)
            if pairs_for is not None
            else [{**pair, "source_chunk_ids": [chunk_ids[0]]} for pair in _QA_PAIRS]
        )
        return _ok_response(json.dumps({"qa_pairs": pairs}, ensure_ascii=False))

    return handler


def _claim_and_run_qa(
    session: Session, *, client: DeepSeekClient, settings: Settings = _SETTINGS
) -> Task:
    task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
    assert task is not None
    session.commit()
    run_qa_planning(session, task, settings=settings, client=client)
    session.commit()
    return task


def test_qa_planning_extracts_pairs_to_units(session_factory: Callable[[], Session]) -> None:
    """问答对 → 生成单元：topic=原问题/原陈述、卡型照录、难度统一 BASIC、tier 为空；
    账本记 qa-planner（stage=PLANNING，operation_key 前缀 planning:qa:）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_qa_task(session, user_id=user)
        task = _claim_and_run_qa(
            session, client=DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(_qa_handler()))
        )
        assert task.task_id == task_id
        assert task.stage == "GENERATING"  # 规划完成 → 生成阶段（下游零改动）
        units = list(
            session.scalars(
                select(KnowledgePoint)
                .where(KnowledgePoint.task_id == task_id)
                .order_by(KnowledgePoint.priority)
            ).all()
        )
        assert [u.topic for u in units] == [
            "什么是上下文窗口？",
            "上下文窗口越长，模型单次能读取的文本越多。",
        ]
        assert [u.card_type for u in units] == ["QUESTION", "TRUE_FALSE"]
        assert all(u.target_difficulty == "BASIC" for u in units)  # 归档参考统一 BASIC
        assert all(u.coverage_tier is None for u in units)
        batches = list(session.scalars(select(Batch).where(Batch.task_id == task_id)).all())
        assert len(batches) == 2  # 1 单元 = 1 批
        attempts = list(
            session.scalars(select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)).all()
        )
        assert attempts and all(a.prompt_name == "qa-planner" for a in attempts)
        assert all(a.stage == "PLANNING" for a in attempts)
        assert all(a.operation_key.startswith("planning:qa:") for a in attempts)


def test_qa_planning_dedupes_normalized_questions(session_factory: Callable[[], Session]) -> None:
    """跨段/重复问答去重：题号噪音差异的同一问题只保留一单元。"""

    def pairs_for(payload: dict[str, Any]) -> list[dict[str, Any]]:
        chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
        return [
            {
                "card_type": "QUESTION",
                "question": "12. 什么是上下文窗口？",
                "answer": "模型一次推理能读取的最大文本长度。",
                "source_chunk_ids": [chunk_ids[0]],
            },
            {
                "card_type": "QUESTION",
                "question": "什么是上下文窗口？",
                "answer": "模型一次推理能读取的最大文本长度。",
                "source_chunk_ids": [chunk_ids[0]],
            },
        ]

    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_qa_task(session, user_id=user)
        task = _claim_and_run_qa(
            session,
            client=DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(_qa_handler(pairs_for))),
        )
        assert task.stage == "GENERATING"
        count = session.scalar(
            select(func.count())
            .select_from(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
        )
        assert count == 1


def test_qa_planning_empty_pairs_completes(session_factory: Callable[[], Session]) -> None:
    """全部段成功但 0 问答对 → COMPLETED + NO_GENERATION_UNITS（叙述性资料非题库）。"""

    def empty(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    user = _uuid()
    with session_factory() as session:
        _seed_qa_task(session, user_id=user)
        task = _claim_and_run_qa(
            session,
            client=DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(_qa_handler(empty))),
        )
        assert task.status == "COMPLETED"
        assert task.completion_reason == "NO_GENERATION_UNITS"


def test_qa_planning_all_operations_failed(session_factory: Callable[[], Session]) -> None:
    """全部提取操作失败（非法输出耗尽预算）→ FAILED + failure_stage=PLANNING。"""
    user = _uuid()
    with session_factory() as session:
        _seed_qa_task(session, user_id=user)
        task = _claim_and_run_qa(
            session,
            client=DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(_qa_handler(invalid=True))
            ),
        )
        assert task.status == "FAILED"
        assert task.failure_stage == "PLANNING"


def test_planning_worker_dispatches_qa_mode(session_factory: Callable[[], Session]) -> None:
    """executor 规划 worker 按 source_mode 分派：QA_DIRECT 任务走 run_qa_planning。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_qa_task(session, user_id=user)
        session.info["settings"] = _SETTINGS
        process_active_tasks(
            session,
            settings=_SETTINGS,
            client_factory=lambda _key: DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(_qa_handler())
            ),
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None
        assert task.stage == "GENERATING"
        units = session.scalars(
            select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)
        ).all()
        assert len(units) == 2
