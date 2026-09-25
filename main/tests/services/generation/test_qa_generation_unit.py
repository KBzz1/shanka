"""问答直通生成侧单测（V25-D-43）：spec 形状、样卡直取、预算密度与配置指纹兼容。"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, KnowledgePoint, Task, TextChunk
from infra.db.session import create_db_engine, create_session_factory
from services.generation.batches import _build_generator_prompts, _task_source_mode
from services.generation.samples import config_fingerprint, sample_cards_llm
from services.tasks.service import _budget_guard

_NOW = "2026-08-15T00:00:00.000Z"
_SETTINGS = Settings(  # type: ignore[call-arg]
    deepseek_api_key="stub",
    api_key_encryption_key="aa" * 32,
    _env_file=None,
)


def _qa_config() -> GenerationConfig:
    return GenerationConfig(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        source_mode="QA_DIRECT",
    )


def _extract_config() -> GenerationConfig:
    return GenerationConfig(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )


def _spec_of(user_prompt: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(user_prompt.split("<GENERATION_SPEC>")[1].split("</GENERATION_SPEC>")[0]),
    )


# ---------- 生成 spec 形状 ----------


def _task_with_config(config: GenerationConfig) -> Task:
    return Task(
        task_id="t-1",
        user_id="u-1",
        file_id="f-1",
        status="GENERATING",
        selected_chapters="[]",
        generation_config=config.model_dump_json(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _unit(card_type: str | None = "QUESTION") -> KnowledgePoint:
    return KnowledgePoint(
        knowledge_point_id="kp-1",
        task_id="t-1",
        chapter_id="ch-1",
        source_chunk_id="c1",
        topic="什么是上下文窗口？",
        priority=1,
        status="PENDING",
        target_difficulty="BASIC",
        card_type=card_type,
        coverage_tier=None,
        source_chunk_ids=json.dumps(["c1"]),
    )


def _pages() -> list[TextChunk]:
    return [
        TextChunk(
            chunk_id="c1",
            file_id="f-1",
            material_id="f-1",
            chunk_seq=1,
            page_number=1,
            char_count=30,
            content_sha256="0" * 64,
            content="1. 什么是上下文窗口？答：模型一次推理能读取的最大文本长度。",
            created_at=_NOW,
        )
    ]


def test_generator_prompts_qa_spec_carries_question_not_objective() -> None:
    system, user = _build_generator_prompts(_task_with_config(_qa_config()), _unit(), _pages())
    spec = _spec_of(user)
    assert spec["source_mode"] == "QA_DIRECT"
    assert spec["question"] == "什么是上下文窗口？"
    assert "learning_objective" not in spec  # 问答直通无命题目标
    assert "题库排版员" in system  # generator-qa v1 资产
    assert "<SOURCE_MATERIAL>" in user


def test_generator_prompts_qa_true_false_uses_statement() -> None:
    _, user = _build_generator_prompts(
        _task_with_config(_qa_config()), _unit(card_type="TRUE_FALSE"), _pages()
    )
    spec = _spec_of(user)
    assert spec["statement"] == "什么是上下文窗口？"
    assert "question" not in spec


def test_generator_prompts_extract_mode_unchanged() -> None:
    system, user = _build_generator_prompts(_task_with_config(_extract_config()), _unit(), _pages())
    spec = _spec_of(user)
    assert spec["learning_objective"] == "什么是上下文窗口？"
    assert "source_mode" not in spec
    assert "闪卡作者" in system  # 既有 generator v7 资产


def test_task_source_mode_tolerates_broken_config() -> None:
    task = _task_with_config(_extract_config())
    task.generation_config = "{broken"
    assert _task_source_mode(task) == "EXTRACT"


# ---------- 样卡直取 ----------


class _QaSampleStub:
    """记录调用并返回合规 QUESTION 卡（样卡直取路径只需一张）。"""

    def __init__(self) -> None:
        self.specs: list[dict[str, Any]] = []
        self.systems: list[str] = []

    def close(self) -> None:
        pass

    def chat(
        self,
        prompt: str,
        api_key: str = "",
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.specs.append(_spec_of(prompt))
        self.systems.append(str(system_prompt))
        return {
            "content": json.dumps(
                {
                    "cards": [
                        {
                            "type": "QUESTION",
                            "question": "什么是上下文窗口？",
                            "answer": "模型一次推理能读取的最大文本长度。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
            "model": "deepseek-flash",
            "http_status": 200,
            "duration_ms": 1,
        }


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'qa_gen.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_chunks(session: Session, *, file_id: str) -> None:
    from infra.db.models import LearningProject, Material, PdfFile, User

    user_id = "u-seed"
    session.add(
        User(
            user_id=user_id,
            username="u-seed",
            email="u-seed@example.com",
            password_hash="x",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    session.flush()
    project = LearningProject(
        project_id="p-1",
        user_id=user_id,
        name="题库项目",
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    session.add(
        PdfFile(
            file_id=file_id,
            user_id=user_id,
            filename="bank.pdf",
            storage_key="seed",
            size_bytes=10,
            status="PARSED",
            created_at=_NOW,
        )
    )
    session.flush()
    session.add(
        Material(material_id=file_id, project_id="p-1", type="PDF", name="b.pdf", created_at=_NOW)
    )
    session.flush()
    session.execute(
        insert(TextChunk).values(
            chunk_id="c-seed",
            file_id=file_id,
            material_id=file_id,
            chunk_seq=1,
            page_number=1,
            char_count=40,
            content_sha256="0" * 64,
            content="1. 什么是上下文窗口？答：模型一次推理能读取的最大文本长度。",
            created_at=_NOW,
        )
    )
    session.flush()


def _qa_task(file_id: str, user_id: str) -> Task:
    return Task(
        task_id=f"t-{file_id}",
        user_id=user_id,
        file_id=file_id,
        status="SAMPLE_GENERATING",
        selected_chapters=json.dumps(
            [{"chapter_id": "ch-1", "name": "第一章", "start_page": 1, "end_page": 1}]
        ),
        generation_config=_qa_config().model_dump_json(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_qa_sample_single_card_with_sample_flag(session_factory: Callable[[], Session]) -> None:
    """QA_DIRECT 样卡：单张（BASIC 标注）、spec 带 sample:true、system 用 generator-qa。"""
    with session_factory() as session:
        _seed_chunks(session, file_id="f-qa")
        task = _qa_task("f-qa", "u-seed")
        session.add(task)
        session.commit()
        stub = _QaSampleStub()
        cards = sample_cards_llm(
            session, task=task, config=_qa_config(), client=stub, settings=_SETTINGS
        )
    assert len(cards) == 1
    assert cards[0]["target_difficulty"] == "BASIC"
    assert len(stub.specs) == 1
    assert stub.specs[0]["sample"] is True
    assert "题库排版员" in stub.systems[0]


def test_extract_sample_still_three_cards(session_factory: Callable[[], Session]) -> None:
    """EXTRACT 样卡不受影响：三档全启用仍 3 张（回归锚点）。"""

    class _ExtractStub(_QaSampleStub):
        def chat(
            self,
            prompt: str,
            api_key: str = "",
            *,
            system_prompt: str | None = None,
            max_tokens: int | None = None,
        ) -> dict[str, Any]:
            spec = _spec_of(prompt)
            difficulty = str(spec.get("target_difficulty"))
            self.specs.append(spec)
            self.systems.append(str(system_prompt))
            return {
                "content": json.dumps(
                    {"cards": [{"type": "QUESTION", "question": f"q-{difficulty}", "answer": "a"}]},
                    ensure_ascii=False,
                ),
                "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
                "model": "deepseek-flash",
                "http_status": 200,
                "duration_ms": 1,
            }

    with session_factory() as session:
        _seed_chunks(session, file_id="f-ex")
        task = Task(
            task_id="t-ex",
            user_id="u-seed",
            file_id="f-ex",
            status="SAMPLE_GENERATING",
            selected_chapters=json.dumps(
                [{"chapter_id": "ch-1", "name": "第一章", "start_page": 1, "end_page": 1}]
            ),
            generation_config=_extract_config().model_dump_json(),
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(task)
        session.commit()
        stub = _ExtractStub()
        cards = sample_cards_llm(
            session, task=task, config=_extract_config(), client=stub, settings=_SETTINGS
        )
    assert len(cards) == 3


# ---------- 预算守卫与配置指纹 ----------


def test_budget_guard_qa_density(session_factory: Callable[[], Session]) -> None:
    """QA_DIRECT 按题库密度估算：10 万字 → 400 对 > 300 上限 → 创建期拒绝。"""
    with session_factory() as session:
        with pytest.raises(AppError) as exc_info:
            _budget_guard(
                session,
                chapter_count=1,
                chapter_chars=100_000,
                config=_qa_config(),
                settings=_SETTINGS,
            )
        assert exc_info.value.code == ErrorCode.VALIDATION_ERROR
        _budget_guard(
            session,
            chapter_count=1,
            chapter_chars=50_000,  # 200 对 ≤ 300
            config=_qa_config(),
            settings=_SETTINGS,
        )


def test_config_fingerprint_source_mode_backward_compatible() -> None:
    """EXTRACT 缺省不进指纹载荷：部署前的 sample_config_hash 对既有任务保持有效；
    QA_DIRECT 指纹天然不同。"""
    legacy_payload: dict[str, object] = {
        "coverage_mode": "BALANCED",
        "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
        "custom_requirements": None,
    }
    assert config_fingerprint(legacy_payload) == config_fingerprint(_extract_config())
    assert config_fingerprint(_extract_config()) != config_fingerprint(_qa_config())
