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
    PdfFile,
    Task,
    User,
)
from infra.db.session import create_db_engine, create_session_factory
from services.generation.samples import config_fingerprint, sample_cards
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
    session.execute(
        insert(ApiKey).values(
            user_id=user_id,
            encrypted_key="enc",
            status="AVAILABLE",
            masked_key="sk-****",
            updated_at=_NOW,
        )
    )
    session.flush()
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

    process_active_tasks(session, settings=_settings())
    session.commit()
    row = session.get(Task, task_id)
    assert row is not None and row.status == "AWAITING_SAMPLE_CONFIRMATION"


def _settings() -> Settings:
    # _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
    return Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]


# ---------- 启用难度 ↔ 样卡数（1~3 张） ----------


def test_sample_cards_only_for_enabled_difficulties() -> None:
    """比例为 0 的难度不生成样卡；1~3 张与启用难度一一对应（契约 3.5）。"""
    chapter = "第一章"
    task_id = _uuid()
    # 三档全启用 → 3 张
    cards = sample_cards(_config(40, 40, 20), chapter_name=chapter, task_id=task_id)
    assert len(cards) == 3
    assert {c["target_difficulty"] for c in cards} == {
        "BASIC",
        "UNDERSTANDING",
        "DEEP_QUESTION",
    }
    # 禁用理解档（0）→ 2 张，不含 UNDERSTANDING
    cards = sample_cards(_config(40, 0, 60), chapter_name=chapter, task_id=task_id)
    assert len(cards) == 2
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "DEEP_QUESTION"}
    # 仅启用基础档（100/0/0）→ 1 张
    cards = sample_cards(_config(100, 0, 0), chapter_name=chapter, task_id=task_id)
    assert len(cards) == 1
    assert {c["target_difficulty"] for c in cards} == {"BASIC"}
    # 每张样卡为合法轻量组件（3.13：card_id/front/back/card_type）
    for card in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(card)


def test_sample_cards_deterministic_per_task() -> None:
    """同配置 + 同章节 + 同任务 → 同 card_id（seed 带任务维度，F-1）。"""
    a = sample_cards(_config(), chapter_name="第一章", task_id="t1")
    b = sample_cards(_config(), chapter_name="第一章", task_id="t1")
    c = sample_cards(_config(), chapter_name="第一章", task_id="t2")
    assert [x["card_id"] for x in a] == [x["card_id"] for x in b]
    assert [x["card_id"] for x in a] != [x["card_id"] for x in c]


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
        process_active_tasks(session, settings=_settings())
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
        process_active_tasks(session, settings=_settings())
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

    def flaky(session: Session, task: Task) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("注入的样卡路径异常（DB/编程错误）")
        return original(session, task)

    monkeypatch.setattr(tasks_executor, "_complete_sample_task", flaky)
    with session_factory() as session:
        process_active_tasks(session, settings=_settings())  # 异常被兜底，不向上传播
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
