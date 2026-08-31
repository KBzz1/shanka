"""V2.5 样卡持久化集成测试（Task 5）：1~3 张（比例>0 难度各 1 张）+ 配置指纹 + 幂等。

覆盖（structure-contract 3.5/4.1/6.4）：
- 禁用难度段（比例 0）不生成样卡；样卡数与启用难度一一对应（1~3 张）；
- 样卡/配置指纹持久化于任务，跨 session 可读（页面切换/App 退出/换设备）；
- sample_config_hash 为确定性配置指纹；配置变更清空样卡与指纹；
- 幂等触发（同幂等键重放不重复转移）；SAMPLE_GENERATING 时 abandon 并发写入无害。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import (
    ApiKey,
    Base,
    Chapter,
    Deck,
    LearningProject,
    LlmCallAttempt,
    PdfFile,
    Task,
    TextChunk,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from services.generation.samples import config_fingerprint, sample_cards_llm
from services.tasks.service import (
    abandon_task,
    create_task,
    request_samples,
    update_task,
)

_NOW = "2026-08-15T00:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'v25_samples.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _config(basic: int = 40, understanding: int = 40, deep: int = 20) -> GenerationConfig:
    return GenerationConfig(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio(
            basic=basic, understanding=understanding, deep_question=deep
        ),
    )


def _seed(session: Session, *, user_id: str) -> dict[str, object]:
    """users 前置 + 项目 + PdfFile PARSED + 2 章节 + 项目绑定牌组 + ApiKey。"""
    if session.get(User, user_id) is None:
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
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=10,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    deck = Deck(
        deck_id=_uuid(),
        user_id=user_id,
        name="D",
        source="MANUAL",
        project_id=project.project_id,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(deck)
    session.flush()
    chapter_ids: list[str] = []
    for i in range(2):
        ch = Chapter(
            chapter_id=_uuid(),
            file_id=pdf.file_id,
            name=f"第{i + 1}章",
            start_page=i + 1,
            end_page=i + 2,
        )
        session.add(ch)
        session.flush()
        chapter_ids.append(ch.chapter_id)
    encryption_key = key_from_settings(_settings())
    assert encryption_key is not None
    session.execute(
        insert(ApiKey).values(
            user_id=user_id,
            encrypted_key=encrypt_key("sk-test-0123456789", encryption_key),
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
    # 样卡真实生成需章节文本：按章节页区间种页文本（第 1 章 1-2 页、第 2 章 2-3 页）
    _seed_chunks(session, file_id=pdf.file_id, pages=3)
    return {
        "project_id": project.project_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
    }


def _create_draft(session: Session, ctx: dict[str, object], *, user_id: str) -> Task:
    return create_task(
        session,
        user_id=user_id,
        project_id=str(ctx["project_id"]),
        deck_id=str(ctx["deck_id"]),
        chapter_ids=[str(c) for c in cast(list[str], ctx["chapter_ids"])],
        config=_config(),
        now=_NOW,
    )


def _complete_samples(session: Session, task_id: str) -> None:
    """样卡 worker 完成（SAMPLE_GENERATING → AWAITING + 持久化样卡与指纹）。"""
    from services.tasks.executor import process_active_tasks

    process_active_tasks(session, settings=_settings(), client_factory=_stub_factory())
    session.commit()
    row = session.get(Task, task_id)
    assert row is not None and row.status == "AWAITING_SAMPLE_CONFIRMATION"


def _settings() -> Settings:
    # _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
    return Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]


def _envelope(user_prompt: str) -> dict[str, object]:
    raw = user_prompt.split("<GENERATION_SPEC>")[1].split("</GENERATION_SPEC>")[0]
    return cast("dict[str, object]", json.loads(raw))


class StubClient:
    """样卡 worker 注入的假 LLM：按信封 target_difficulty 返回合规 QUESTION 卡（不触网）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def close(self) -> None:
        pass

    def chat(
        self,
        prompt: str,
        api_key: str = "",
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        payload = _envelope(prompt)
        difficulty = str(payload["target_difficulty"])
        self.calls.append(difficulty)
        return {
            "content": json.dumps(
                {
                    "cards": [
                        {
                            "type": "QUESTION",
                            "question": f"样卡问题-{difficulty}",
                            "answer": f"样卡答案-{difficulty}",
                        }
                    ]
                }
            ),
            "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
            "model": "deepseek-v4-flash",
            "http_status": 200,
            "duration_ms": 1,
        }


def _seed_file(session: Session) -> str:
    """User + PdfFile 行（text_chunks FK 前置），返回 file_id。"""
    user_id = _uuid()
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
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=10,
        status="PARSED",
        created_at=_NOW,
    )
    session.add(pdf)
    session.flush()
    return pdf.file_id


def _seed_chunks(session: Session, *, file_id: str, pages: int = 3) -> None:
    for page in range(1, pages + 1):
        session.execute(
            insert(TextChunk).values(
                chunk_id=_uuid(),
                file_id=file_id,
                page_number=page,
                char_count=20,
                content_sha256="0" * 64,
                content=f"第 {page} 页：上下文工程核心概念。",
                created_at=_NOW,
            )
        )
    session.flush()


def _stub_factory() -> Callable[[str], StubClient]:
    return lambda _key: StubClient()


# ---------- 启用难度 ↔ 样卡数（1~3 张） ----------


def _llm_task(config: GenerationConfig, *, file_id: str) -> Task:
    return Task(
        task_id=_uuid(),
        user_id=_uuid(),
        file_id=file_id,
        status="SAMPLE_GENERATING",
        selected_chapters=json.dumps(
            [
                {
                    "chapter_id": _uuid(),
                    "name": "第一章",
                    "start_page": 1,
                    "end_page": 2,
                }
            ]
        ),
        generation_config=config.model_dump_json(),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_sample_cards_only_for_enabled_difficulties(
    session_factory: Callable[[], Session],
) -> None:
    """比例为 0 的难度不生成样卡；1~3 张与启用难度一一对应（契约 3.5，真实生成路径）。"""
    with session_factory() as session:
        file_id = _seed_file(session)
        _seed_chunks(session, file_id=file_id)
        client = StubClient()
        # 三档全启用 → 3 张
        cards = sample_cards_llm(
            session,
            task=_llm_task(_config(40, 40, 20), file_id=file_id),
            config=_config(40, 40, 20),
            client=client,
            settings=_settings(),
        )
        assert len(cards) == 3
        assert {c["target_difficulty"] for c in cards} == {
            "BASIC",
            "UNDERSTANDING",
            "DEEP_QUESTION",
        }
        # 禁用理解档（0）→ 2 张，不含 UNDERSTANDING
        cards = sample_cards_llm(
            session,
            task=_llm_task(_config(40, 0, 60), file_id=file_id),
            config=_config(40, 0, 60),
            client=client,
            settings=_settings(),
        )
        assert len(cards) == 2
        assert {c["target_difficulty"] for c in cards} == {"BASIC", "DEEP_QUESTION"}
        # 仅启用基础档（100/0/0）→ 1 张
        cards = sample_cards_llm(
            session,
            task=_llm_task(_config(100, 0, 0), file_id=file_id),
            config=_config(100, 0, 0),
            client=client,
            settings=_settings(),
        )
        assert len(cards) == 1
        assert {c["target_difficulty"] for c in cards} == {"BASIC"}
        # 每张样卡为合法轻量组件（3.13：card_id/front/back/card_type）
        for card in cards:
            assert {"card_id", "front", "back", "card_type"} <= set(card)


def test_sample_cards_fresh_ids_per_call(
    session_factory: Callable[[], Session],
) -> None:
    """LLM 样卡每次生成全新 card_id/generation_item_id（V5A 真实生成语义，无确定性 seed）。"""
    with session_factory() as session:
        file_id = _seed_file(session)
        _seed_chunks(session, file_id=file_id)
        a = sample_cards_llm(
            session,
            task=_llm_task(_config(), file_id=file_id),
            config=_config(),
            client=StubClient(),
            settings=_settings(),
        )
        b = sample_cards_llm(
            session,
            task=_llm_task(_config(), file_id=file_id),
            config=_config(),
            client=StubClient(),
            settings=_settings(),
        )
    assert [x["card_id"] for x in a] != [x["card_id"] for x in b]
    for card in a + b:
        assert card["source"] == "GENERATED"
        assert card["generation_item_id"]


def test_sample_config_fingerprint_deterministic() -> None:
    """配置指纹确定性：同配置同值；不同配置不同值。"""
    assert config_fingerprint(_config()) == config_fingerprint(_config())
    assert config_fingerprint(_config(50, 30, 20)) != config_fingerprint(_config(40, 40, 20))
    assert config_fingerprint(_config()) == config_fingerprint(_config().model_dump())


# ---------- 持久化：跨 session / 幂等 / 配置变更 / 并发 abandon ----------


def test_samples_persist_across_sessions(session_factory: Callable[[], Session]) -> None:
    """样卡 + 指纹持久化于任务：页面切换/App 退出后读取继续（无需重新生成）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
    with session_factory() as session:
        _complete_samples(session, task_id)
    with session_factory() as session:  # "新设备"：全新 session 读取
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "AWAITING_SAMPLE_CONFIRMATION"
        cards = json.loads(row.sample_cards or "[]")
        assert len(cards) == 3
        assert row.sample_config_hash == config_fingerprint(_config())
        assert row.sample_confirmed_at is None  # 确认发生在 start


def test_sample_success_ledger_is_reused_after_worker_restart(
    session_factory: Callable[[], Session],
) -> None:
    """同一任务的样卡成功账本可跨调用复用，已成功难度不再次调用模型。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()

    with session_factory() as session:
        first = session.get(Task, task_id)
        assert first is not None
        first_client = StubClient()
        first_cards = sample_cards_llm(
            session,
            task=first,
            config=_config(),
            client=first_client,
            settings=_settings(),
        )
        session.commit()
        assert len(first_client.calls) == 3

    with session_factory() as session:
        restarted = session.get(Task, task_id)
        assert restarted is not None
        restarted_client = StubClient()
        second_cards = sample_cards_llm(
            session,
            task=restarted,
            config=_config(),
            client=restarted_client,
            settings=_settings(),
        )
        attempts = session.scalars(
            select(LlmCallAttempt).where(
                LlmCallAttempt.task_id == task_id,
                LlmCallAttempt.stage == "SAMPLE",
            )
        ).all()
    assert restarted_client.calls == []
    assert [card["card_id"] for card in second_cards] == [card["card_id"] for card in first_cards]
    assert len(attempts) == 3
    assert {attempt.status for attempt in attempts} == {"SUCCESS"}


def test_samples_trigger_idempotent_replay_no_double_transition(
    session_factory: Callable[[], Session],
) -> None:
    """同幂等键重放 → 服务层不重复转移（首次 DRAFT→SAMPLE_GENERATING 后重放 409，
    HTTP 幂等层由同键快照兜底——见 test_v25_task_lifecycle HTTP 层用例）。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        first = request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert first.status == "SAMPLE_GENERATING"
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
    # SAMPLE_GENERATING 中 abandon 仍合法（正式生成前）
    with session_factory() as session:
        result = abandon_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        assert result.status == "ABANDONED"


def test_config_change_clears_persisted_samples(
    session_factory: Callable[[], Session],
) -> None:
    """配置变更后样卡失效：sample_cards/sample_config_hash/sample_confirmed_at 清空。"""
    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        _complete_samples(session, task_id)
    with session_factory() as session:
        result = update_task(
            session,
            user_id=user,
            task_id=task_id,
            config=_config(50, 30, 20),
            now=_NOW,
        )
        session.commit()
        assert result.status == "DRAFT"
        assert result.sample_cards is None
        assert result.sample_config_hash is None
        assert result.sample_confirmed_at is None


def test_abandon_during_sample_generating_write_harmless(
    session_factory: Callable[[], Session],
) -> None:
    """SAMPLE_GENERATING 时并发 abandon：后台样卡写入不复活任务（条件更新 CAS）。"""
    from services.tasks.executor import process_active_tasks

    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
    # "后台请求"进行中 abandon（先落 ABANDONED，再让 worker 扫描）
    with session_factory() as session:
        abandon_task(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
    with session_factory() as session:
        process_active_tasks(session, settings=_settings(), client_factory=_stub_factory())
        session.commit()
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "ABANDONED"  # 不复活
        assert row.sample_cards is None  # 不写样卡


def test_sample_worker_failure_marks_failed(
    session_factory: Callable[[], Session],
) -> None:
    """防御路径：样卡生成不可恢复错误 → FAILED（可重试/删除，不悬挂 SAMPLE_GENERATING）。"""
    from services.tasks.executor import process_active_tasks

    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        task = _create_draft(session, ctx, user_id=user)
        session.commit()
        task_id = task.task_id
        request_samples(session, user_id=user, task_id=task_id, now=_NOW)
        session.commit()
        # 破坏样卡生成输入：清空章节快照（生成无法取章节名）
        session.execute(update(Task).where(Task.task_id == task_id).values(selected_chapters="[]"))
        session.commit()
    with session_factory() as session:
        process_active_tasks(session, settings=_settings(), client_factory=_stub_factory())
        session.commit()
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "FAILED"
        assert row.error_code is not None


def test_sample_worker_unexpected_error_fails_task_and_continues_round(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-1 守卫（评审 R1）：样卡路径非输入类异常（编程/DB 错误——_complete_sample_task
    内层仅捕 ValueError/TypeError/KeyError）→ 该任务 FAILED 兜底，同轮其余任务照常完成
    （不中止整轮扫描、不饿死后继任务）。样卡阶段失败不写 failure_stage（M-5 裁决：
    枚举无 SAMPLE_GENERATING 对应值，NULL 即正确值）。"""
    import services.tasks.executor as tasks_executor
    from services.tasks.executor import process_active_tasks

    user = _uuid()
    with session_factory() as session:
        ctx = _seed(session, user_id=user)
        session.commit()
    with session_factory() as session:
        for _ in range(2):
            task = _create_draft(session, ctx, user_id=user)
            session.commit()
            task_id = task.task_id
            request_samples(session, user_id=user, task_id=task_id, now=_NOW)
            session.commit()
    # 注入：单任务样卡路径抛非输入类异常（R2：补丁目标取 executor 本模块定义的
    # _complete_sample_task——complete_samples 为 executor 从 service 再导入的名字，
    # 非模块显式导出，mypy 全仓库 implicit-reexport 会拒绝 patch；语义等价，异常
    # 同样可逃逸至 worker 层守卫）
    original = tasks_executor._complete_sample_task
    calls = {"n": 0}

    def flaky(
        session: Session,
        task: Task,
        settings: Settings,
        client_factory: Callable[[str], StubClient] | None = None,
    ) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("注入的样卡路径异常（DB/编程错误）")
        return original(session, task, settings, client_factory=client_factory)

    monkeypatch.setattr(tasks_executor, "_complete_sample_task", flaky)
    with session_factory() as session:
        process_active_tasks(  # 异常被兜底，不向上传播
            session, settings=_settings(), client_factory=_stub_factory()
        )
        session.commit()
    with session_factory() as session:
        rows = session.scalars(select(Task)).all()
        assert {t.status for t in rows} == {"FAILED", "AWAITING_SAMPLE_CONFIRMATION"}
        failed = next(t for t in rows if t.status == "FAILED")
        awaiting = next(t for t in rows if t.status == "AWAITING_SAMPLE_CONFIRMATION")
        assert failed.error_code == ErrorCode.GENERATION_FAILED.value
        assert failed.failure_stage is None  # M-5 裁决：样卡阶段失败不写 failure_stage
        assert awaiting.sample_cards is not None  # 同轮其余任务照常完成
        assert calls["n"] == 2  # 两个任务都被处理（第二个未被饿死）
