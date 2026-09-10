"""两阶段规划执行集成测试（spec §6.1/§6.2/§6.4；V2.5.2 粗+精）。

基座同 test_tasks_executor.py：真实 SQLite 全表建库 + mock transport client。mock chat
按 user message 信封分派：<PLANNER_COARSE_INPUT> → 主题清单响应；<PLANNER_INPUT> →
按 payload.topics 逐主题展开单元（引用合法来源与 topic_index）。

页文本默认加厚到 750 字/页（2 页 1500 字 → COMPACT 主题区间 [2,2]），保证默认用例
能规划出 2 主题/2 单元——薄内容在密度制下合法地产出更少。
"""

import json
import logging
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
from infra.llm.prompts import asset_versions
from services.generation.ledger import create_attempt, finish_success
from services.generation.planning_executor import (
    claim_planning_task,
    coarse_fingerprint,
    fine_fingerprint,
    run_planning,
)
from services.generation.quota import difficulty_interval, interval_for_chapter
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.service import create_task

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_NOW = "2026-08-12T00:00:00.000Z"
_CLAIM_NOW = "2026-08-12T01:00:00.000Z"


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'planning.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _page_content(page_number: int) -> str:
    """确定性页文本（约 750 字/页；chunk_id 由 (file_id, page, content) 决定，可复算）。"""
    return f"第{page_number}页内容" * 150


def _anchors(settings: Settings) -> dict[str, float]:
    return {
        "COMPACT": settings.cards_per_10k_compact,
        "BALANCED": settings.cards_per_10k_balanced,
        "EXTENSIVE": settings.cards_per_10k_extensive,
    }


def _seed_planning_task(
    session: Session,
    *,
    user_id: str,
    chapter_start_page: int = 1,
    chapter_end_page: int = 2,
    text_page_range: tuple[int, int] | None = None,
    coverage_mode: str = "COMPACT",
) -> tuple[str, str, str]:
    """GENERATING+PLANNING 任务（start 后状态）+ 章节 + 页文本（text_chunks）；
    返回 (task_id, chapter_id, file_id)。

    text_page_range 覆盖页文本落库范围（缺省 = 章节页码范围）；可构造"章节无文本"场景。
    """
    from services.decks.service import create_deck

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
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        name="P",
        chapters_confirmed_at=_NOW,
        version=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )
    session.add(project)
    session.flush()
    session.add(
        Material(
            material_id=pdf.file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
            project_id=project.project_id,
            type="PDF",
            name="seed.pdf",
            status=None,
            created_at=_NOW,
        )
    )
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now=_NOW)
    deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
    session.flush()
    ch = Chapter(
        chapter_id=_uuid(),
        file_id=pdf.file_id,
        material_id=pdf.file_id,
        name="第一章",
        start_page=chapter_start_page,
        end_page=chapter_end_page,
    )
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
    text_range = text_page_range or (chapter_start_page, chapter_end_page)
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[
            {"page_number": pn, "content": _page_content(pn)}
            for pn in range(text_range[0], text_range[1] + 1)
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
            coverage_mode=coverage_mode,
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        ),
        now=_NOW,
    )
    # start 后状态（4.1）：AWAITING_SAMPLE_CONFIRMATION → GENERATING + stage=PLANNING
    task.status = "GENERATING"
    task.stage = "PLANNING"
    task.updated_at = _NOW
    session.commit()
    return task.task_id, ch.chapter_id, pdf.file_id


def _parse_user_payload(request: httpx.Request) -> dict[str, Any]:
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    for tag in ("<PLANNER_COARSE_INPUT>", "<PLANNER_INPUT>"):
        if tag in user:
            payload: dict[str, Any] = json.loads(
                user.split(tag, 1)[1].split(tag.replace("<", "</"), 1)[0]
            )
            return payload
    raise AssertionError(f"未知规划信封: {user[:60]}")


def _default_coarse_topics(
    payload: dict[str, Any], *, tiers: list[str] | None = None
) -> list[dict[str, Any]]:
    """默认粗规划响应：2 个主题（COMPACT 安全的 CORE 层级），引用首两页。"""
    chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
    tier_list = tiers or ["CORE", "CORE"]
    return [
        {
            "title": f"主题{label}",
            "coverage_tier": tier,
            "source_chunk_ids": [chunk_ids[i % len(chunk_ids)]],
        }
        for i, (label, tier) in enumerate(zip(("一", "二"), tier_list), start=0)
    ]


def _default_fine_units(
    payload: dict[str, Any], *, skip_topic: int | None = None
) -> list[dict[str, Any]]:
    """默认精规划响应：每主题 1 单元（难度轮转 BASIC/UNDERSTANDING，引用主题首来源）。"""
    difficulties = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]
    units = []
    for i, topic in enumerate(payload["topics"]):
        if skip_topic is not None and topic["topic_index"] == skip_topic:
            continue  # 模拟漏挖（触发 fine-wide 恢复）
        units.append(
            {
                "topic_index": topic["topic_index"],
                "source_chunk_ids": [topic["source_chunk_ids"][0]],
                "learning_objective": f"说出主题{topic['topic_index']}的核心要点",
                "target_difficulty": difficulties[i % len(difficulties)],
                "card_type": "QUESTION",
            }
        )
    return units


def _two_stage_handler(
    state: dict[str, int],
    *,
    coarse: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
    fine: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """按信封分派的两阶段 mock：coarse/fine 可注入自定义响应构造器。"""

    def read_marker(request: httpx.Request) -> str:
        user = json.loads(request.content)["messages"][-1]["content"]
        return "<PLANNER_COARSE_INPUT>" if "<PLANNER_COARSE_INPUT>" in user else "<PLANNER_INPUT>"

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        payload = _parse_user_payload(request)
        if read_marker(request) == "<PLANNER_COARSE_INPUT>":
            topics = coarse(payload) if coarse else _default_coarse_topics(payload)
            return _ok_response(json.dumps({"topics": topics}, ensure_ascii=False))
        units = fine(payload) if fine else _default_fine_units(payload)
        return _ok_response(json.dumps({"units": units}, ensure_ascii=False))

    return handler


def _client_with_handler(handler: Callable[[httpx.Request], httpx.Response]) -> DeepSeekClient:
    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


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
            "model": "deepseek-v4-flash",
        },
    )


def _claim_and_plan(
    session: Session, *, settings: Settings = _SETTINGS, client: DeepSeekClient
) -> Task:
    task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
    assert task is not None
    session.commit()
    run_planning(session, task, settings=settings, client=client)
    session.commit()
    return task


def _load_pages(session: Session, file_id: str) -> list[Any]:
    from infra.db.models import TextChunk

    return list(
        session.scalars(
            select(TextChunk).where(TextChunk.file_id == file_id).order_by(TextChunk.page_number)
        ).all()
    )


# ---------- CAS 抢占与快照冻结 ----------


def test_claim_cas1_snapshot_freeze(
    session_factory: Callable[[], Session],
) -> None:
    """CAS1：GENERATING+PLANNING（started_at 空）→ 接管落 started_at/心跳，用户状态
    不转移（V2.5 全程 GENERATING）；claim 前修改 Chapter.start_page → 快照含新值。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, _ = _seed_planning_task(session, user_id=user)
        ch = session.get(Chapter, chapter_id)
        assert ch is not None
        ch.start_page = 99
        session.commit()
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        assert task.task_id == task_id
        assert task.status == "GENERATING"  # 接管不转移用户状态（4.1）
        assert task.started_at == _CLAIM_NOW
        snapshot = json.loads(task.selected_chapters)
        assert snapshot[0]["start_page"] == 99  # 重读章节最新页码覆盖快照（§4.2 冻结）
        session.commit()
        # CAS2：GENERATING+PLANNING 但未超时（updated_at == now）→ 拒绝接管
        assert claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW) is None


def test_claim_cas2_orphan_takeover_marks_started_unknown(
    session_factory: Callable[[], Session],
) -> None:
    """CAS2：GENERATING+PLANNING 心跳超时（曾被接管）→ 接管 + 遗留 STARTED 转
    UNKNOWN（恢复按账本）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, _ = _seed_planning_task(session, user_id=user)
        task = session.get(Task, task_id)
        assert task is not None
        task.status = "GENERATING"
        task.stage = "PLANNING"
        task.started_at = "2026-08-12T00:00:00.000Z"  # 曾被接管（CAS2 前置）
        task.updated_at = "2026-08-12T00:00:00.000Z"  # 心跳超时（>30 分钟）
        session.commit()
        create_attempt(
            session,
            user_id=user,
            scope_type="TASK",
            scope_id=task_id,
            task_id=task_id,
            stage="PLANNING",
            operation_key=f"planning:coarse:{chapter_id}:0",
            input_fingerprint="fp-stale",
            attempt_no=1,
            model="m",
            prompt_name="planner-coarse",
            prompt_version="v7",
            now="2026-08-12T00:00:00.000Z",
        )
        session.commit()
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None and task.task_id == task_id
        assert task.updated_at == _CLAIM_NOW  # 接管心跳
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
        assert [a.status for a in attempts] == ["UNKNOWN"]  # 遗留 STARTED → UNKNOWN


def test_claim_chapter_deleted_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """CAS1 提交前章节已删除 → 同事务 FAILED + failure_stage=PLANNING（不接管规划）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, _ = _seed_planning_task(session, user_id=user)
        ch = session.get(Chapter, chapter_id)
        assert ch is not None
        session.delete(ch)
        session.commit()
        assert claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW) is None
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "FAILED"
        assert task.failure_stage == "PLANNING"
        assert task.error_code == "GENERATION_FAILED"


# ---------- 规划执行（两阶段） ----------


def test_planning_success_units_and_batches(
    session_factory: Callable[[], Session],
) -> None:
    """成功规划 → GENERATING + KnowledgePoint（tier 注入/兼容投影）+ 每单元一批。"""
    user = _uuid()
    state: dict[str, int] = {"calls": 0}
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(
            select(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
            .order_by(KnowledgePoint.priority)
        ).all()
        batches = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
    assert state["calls"] == 2  # 1 粗规划 + 1 精规划批
    assert task.stage == "GENERATING"
    assert task.status == "GENERATING"  # V2.5 全程 GENERATING（规划完成不转移用户状态）
    assert task.skipped_planning_group_count == 0
    assert len(kps) == 2  # 2 主题各 1 单元（BASIC + UNDERSTANDING，区间内）
    assert [kp.target_difficulty for kp in kps] == ["BASIC", "UNDERSTANDING"]
    assert all(kp.card_type == "QUESTION" for kp in kps)
    assert all(kp.coverage_tier == "CORE" for kp in kps)  # 服务端从主题注入
    assert all(
        json.loads(kp.source_chunk_ids or "[]")[0] == kp.source_chunk_id for kp in kps
    )  # 兼容投影（spec §3.1）
    assert [kp.priority for kp in kps] == [1, 2]
    assert len(batches) == len(kps)  # 1 单元 1 批
    assert [b.generation_unit_id for b in batches] == [kp.knowledge_point_id for kp in kps]
    assert task.total_batch_count == 2
    assert task.completed_batch_count == 0
    cursor = json.loads(task.cursor) if task.cursor else None
    assert cursor is not None and cursor["difficulty_distribution"] == {
        "BASIC": 1,
        "UNDERSTANDING": 1,
        "DEEP_QUESTION": 0,
    }


def test_planning_compact_filters_disallowed_tiers(
    session_factory: Callable[[], Session],
) -> None:
    """COMPACT 混入 IMPORTANT/LOW_FREQUENCY 主题 → 服务端确定性过滤，仅 CORE 落库。"""
    user = _uuid()
    state: dict[str, int] = {"calls": 0}
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user, coverage_mode="COMPACT")
        handler = _two_stage_handler(
            state, coarse=lambda p: _default_coarse_topics(p, tiers=["CORE", "IMPORTANT"])
        )
        _claim_and_plan(session, client=_client_with_handler(handler))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert state["calls"] == 2
    assert all(kp.coverage_tier == "CORE" for kp in kps)  # IMPORTANT 主题被过滤
    assert len(kps) == 1  # 只剩 CORE 主题 → 1 单元
    assert {a.prompt_name for a in attempts} == {"planner-coarse", "planner"}


def test_planning_empty_topic_recovery_fine_wide(
    session_factory: Callable[[], Session],
) -> None:
    """精规划漏挖主题（批成功但 topic 2 为 0 单元）→ fine-wide 恢复调用补挖，2 单元齐。"""
    user = _uuid()
    state: dict[str, int] = {"calls": 0}
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)

        def fine(payload: dict[str, Any]) -> list[dict[str, Any]]:
            # 批调用（含 2 主题）漏挖 topic 2；fine-wide 恢复调用（单主题）正常产出
            if len(payload["topics"]) > 1:
                return _default_fine_units(payload, skip_topic=2)
            return _default_fine_units(payload)

        handler = _two_stage_handler(state, fine=fine)
        _claim_and_plan(session, client=_client_with_handler(handler))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(
            select(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
            .order_by(KnowledgePoint.priority)
        ).all()
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert state["calls"] == 3  # 粗 + 批 + fine-wide 恢复
    assert len(kps) == 2  # topic 2 经恢复补回
    assert any("fine-wide" in a.operation_key and a.operation_key.endswith(":2") for a in attempts)


def test_planning_success_reuses_normalized(
    session_factory: Callable[[], Session],
) -> None:
    """账本已有粗+精同 operation_key+fingerprint 的 SUCCESS → 全链复用，0 次调用。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(session, user_id=user)
        pages = _load_pages(session, file_id)
        total_chars = sum(p.char_count for p in pages)
        # 镜像 run_planning 推导：2 页 1500 字 → 单粗规划段 [2,2]；单精规划批（全页窗口）
        seg_interval = interval_for_chapter(total_chars, "COMPACT", _anchors(_SETTINGS))
        versions = asset_versions()
        coarse_fp = coarse_fingerprint(pages, seg_interval, "COMPACT", versions)
        topics = [
            {
                "title": "主题一",
                "coverage_tier": "CORE",
                "source_chunk_ids": [pages[0].chunk_id],
                "topic_index": 1,
            },
            {
                "title": "主题二",
                "coverage_tier": "CORE",
                "source_chunk_ids": [pages[1].chunk_id],
                "topic_index": 2,
            },
        ]
        batch_interval = difficulty_interval(seg_interval, 0.4, 0.4, 0.2)
        fine_fp = fine_fingerprint(topics, pages, batch_interval, "COMPACT", versions)
        units = [
            {
                "topic_index": 1,
                "source_chunk_ids": [pages[0].chunk_id],
                "learning_objective": "复用目标一",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "coverage_tier": "CORE",
                "priority": 1,
            },
            {
                "topic_index": 2,
                "source_chunk_ids": [pages[1].chunk_id],
                "learning_objective": "复用目标二",
                "target_difficulty": "UNDERSTANDING",
                "card_type": "QUESTION",
                "coverage_tier": "CORE",
                "priority": 2,
            },
        ]
        for op_key, fp, normalized in (
            (f"planning:coarse:{chapter_id}:0", coarse_fp, json.dumps(topics, ensure_ascii=False)),
            (f"planning:fine:{chapter_id}:0", fine_fp, json.dumps(units, ensure_ascii=False)),
        ):
            attempt = create_attempt(
                session,
                user_id=user,
                scope_type="TASK",
                scope_id=task_id,
                task_id=task_id,
                stage="PLANNING",
                operation_key=op_key,
                input_fingerprint=fp,
                attempt_no=1,
                model="m",
                prompt_name="planner",
                prompt_version="v7",
                now=_NOW,
            )
            finish_success(
                session,
                attempt,
                usage={
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 1,
                    "completion_tokens": 1,
                },
                http_status=200,
                duration_ms=1,
                normalized_result=normalized,
                now=_NOW,
            )
        session.commit()
        state: dict[str, int] = {"calls": 0}
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert state["calls"] == 0  # find_success_result 命中复用，不重复付费调用
    assert task.stage == "GENERATING"
    assert len(kps) == 2
    assert {kp.topic for kp in kps} == {"复用目标一", "复用目标二"}


def test_planning_budget_reset_prevented(
    session_factory: Callable[[], Session],
) -> None:
    """账本已有 3 次尝试（预算耗尽）→ 粗规划操作 SKIPPED、0 次调用、skipped 计数。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(session, user_id=user)
        pages = _load_pages(session, file_id)
        seg_interval = interval_for_chapter(
            sum(p.char_count for p in pages), "COMPACT", _anchors(_SETTINGS)
        )
        op_key = f"planning:coarse:{chapter_id}:0"
        fp = coarse_fingerprint(pages, seg_interval, "COMPACT", asset_versions())
        for attempt_no in (1, 2, 3):
            att = create_attempt(
                session,
                user_id=user,
                scope_type="TASK",
                scope_id=task_id,
                task_id=task_id,
                stage="PLANNING",
                operation_key=op_key,
                input_fingerprint=fp,
                attempt_no=attempt_no,
                model="m",
                prompt_name="planner-coarse",
                prompt_version="v7",
                now=_NOW,
            )
            # STARTED/FAILED/UNKNOWN 任意组合均计入预算（spec §9）
            att.status = ("STARTED", "FAILED", "UNKNOWN")[attempt_no - 1]
            att.finished_at = _NOW
        session.commit()
        state: dict[str, int] = {"calls": 0}
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 0
    assert task.status == "FAILED"  # 全部操作 SKIPPED → §6.4 分支 2
    assert task.failure_stage == "PLANNING"
    assert task.skipped_planning_group_count == 1


def test_planning_empty_units_completed_no_units(
    session_factory: Callable[[], Session],
) -> None:
    """粗规划成功但 0 主题 → COMPLETED + NO_GENERATION_UNITS（§6.4 分支 1）。"""
    user = _uuid()
    state: dict[str, int] = {"calls": 0}
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        handler = _two_stage_handler(state, coarse=lambda p: [])
        _claim_and_plan(session, client=_client_with_handler(handler))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 1  # 仅粗规划调用，无主题不分批
    assert task.status == "COMPLETED"
    assert task.completion_reason == "NO_GENERATION_UNITS"
    assert task.total_batch_count == 0
    assert task.completed_batch_count == 0
    assert task.generated_card_count == 0
    assert task.resumable == 0


def test_planning_all_failed_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """上游持续失败（retryable）→ 3 次尝试后粗规划 SKIPPED、无主题 → FAILED+PLANNING。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500, json={"error": {"message": "upstream down"}})

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert calls == 6  # 3 次逻辑尝试 × 2 次 HTTP（T17 起 adapter 内部重试 1 次/逻辑调用）
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.skipped_planning_group_count == 1
    assert [a.status for a in attempts] == ["FAILED", "FAILED", "FAILED"]


def test_planning_failed_final_condition_update(
    session_factory: Callable[[], Session],
) -> None:
    """粗规划成功后在 guard 前并发 FAILED（另一 worker 系统级失败）→ 条件更新
    rowcount=0 → 不写 KnowledgePoint/Batch（终态守卫以 FAILED 等价验证）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        state: dict[str, int] = {"calls": 0}
        injected = False
        original_refresh = session.refresh

        def refresh_with_cancel(
            instance: object,
            attribute_names: Any = None,
            with_for_update: Any = None,
        ) -> None:
            nonlocal injected
            # 首次 chat 完成后的 Task 刷新 → 注入并发 FAILED（另一连接）
            if not injected and isinstance(instance, Task) and state["calls"] >= 1:
                injected = True
                with session_factory() as cancel_session:
                    task_row = cancel_session.get(Task, task_id)
                    assert task_row is not None
                    task_row.status = "FAILED"
                    task_row.ended_at = _NOW
                    task_row.updated_at = _NOW
                    cancel_session.commit()
            original_refresh(instance, attribute_names, with_for_update)

        session.refresh = refresh_with_cancel  # type: ignore[method-assign]
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kp_count = session.scalar(
            select(func.count())
            .select_from(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
        )
        batch_count = session.scalar(
            select(func.count()).select_from(Batch).where(Batch.task_id == task_id)
        )
    assert state["calls"] == 1  # 粗规划已调用
    assert task.status == "FAILED"  # 并发终态不被最终事务覆盖
    assert kp_count == 0  # 条件不成立 → 整事务回滚
    assert batch_count == 0


def test_planning_coarse_split_and_merge(
    session_factory: Callable[[], Session],
) -> None:
    """粗规划按 planner_coarse_max_input_chars 连续页分段：2 段各一次调用、章内合并去重
    后按章区间截断，精规划单批展开。"""
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_coarse_max_input_chars=1600,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(
            session, user_id=user, chapter_start_page=1, chapter_end_page=4
        )
        received: list[list[str]] = []
        state: dict[str, int] = {"calls": 0}

        def coarse(payload: dict[str, Any]) -> list[dict[str, Any]]:
            chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
            received.append(chunk_ids)
            # 每段 2 个主题（段区间 [2,2]），标题带页锚避免跨段合并去重误伤
            return [
                {
                    "title": f"主题{chunk_ids[i][:8]}",
                    "coverage_tier": "CORE",
                    "source_chunk_ids": [chunk_ids[i]],
                }
                for i in range(min(2, len(chunk_ids)))
            ]

        handler = _two_stage_handler(state, coarse=coarse)
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=_client_with_handler(handler))
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert state["calls"] == 3  # 2 粗规划段 + 1 精规划批
    assert [len(pages) for pages in received] == [2, 2]  # 连续页分段：[1,2] + [3,4]
    assert all(received[0][i] != received[1][0] for i in range(2))  # 段间无重叠页
    # 章区间 [3,3]（3000 字 COMPACT）→ 合并后截断到 3 主题 → 3 单元
    assert len(kps) == 3
    assert task.total_batch_count == 3


def test_planning_hard_cap_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """精规划批数 > max_planner_groups_per_task → 任务 FAILED + PLANNING（批调用前拦截）。"""
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_fine_topics_per_call=1,  # 2 主题 → 2 批
        max_planner_groups_per_task=1,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        state: dict[str, int] = {"calls": 0}
        handler = _two_stage_handler(state)
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=_client_with_handler(handler))
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 1  # 粗规划已执行（2 主题），批数超限在精规划前失败
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.error_code == "GENERATION_FAILED"


def test_planning_no_text_chapter_is_empty_success(
    session_factory: Callable[[], Session],
) -> None:
    """章节范围内无页文本 → 不发请求、成功空结果 → COMPLETED NO_GENERATION_UNITS。"""
    user = _uuid()
    with session_factory() as session:
        # 章节页码 10-12 无对应页文本（text_chunks 只落 1-2 页）→ 无文本成功空结果
        task_id, _, _ = _seed_planning_task(
            session,
            user_id=user,
            chapter_start_page=10,
            chapter_end_page=12,
            text_page_range=(1, 2),
        )
        state: dict[str, int] = {"calls": 0}
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 0
    assert task.status == "COMPLETED"
    assert task.completion_reason == "NO_GENERATION_UNITS"


# ---------- review fix 覆盖测试（1-4） ----------


def test_planning_heartbeat_refreshes_per_attempt(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """review fix 1：心跳时钟每次尝试/终态取新读数——不冻结在 run 起始时刻。

    FrozenClock 每次读取 +2 分钟：首次读数（run 起始）= 01:02，终态 updated_at 必须
    使用更晚读数（否则长运行任务会被 CAS2 按 30 分钟孤儿窗口误判接管）。
    """
    from datetime import UTC, datetime, timedelta

    import services.generation.planning_executor as planning_mod
    from infra.clock import FrozenClock

    base = datetime(2026, 8, 12, 1, 0, 0, tzinfo=UTC)
    steps = iter(FrozenClock(base + timedelta(minutes=2 * i)) for i in range(1, 60))
    monkeypatch.setattr(planning_mod, "SystemClock", lambda: next(steps))

    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        state: dict[str, int] = {"calls": 0}
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
    assert task is not None
    assert task.stage == "GENERATING"
    # 首次读数 = 01:02（run 起始）；终态心跳必须推进到更晚读数
    assert task.updated_at is not None
    assert task.updated_at > "2026-08-12T01:02:00.000Z"


def test_planning_key_error_fail_race_preserves_failed(
    session_factory: Callable[[], Session],
) -> None:
    """review fix 2：401 Key 错误路径的条件更新——finish 提交后、guard 前并发 FAILED
    → rowcount=0 → guard 的 FAILED 不覆盖并发终态（以 FAILED 等价验证）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        chatted = False
        injected = False
        original_refresh = session.refresh

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal chatted
            chatted = True
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})

        def refresh_with_cancel(
            instance: object,
            attribute_names: Any = None,
            with_for_update: Any = None,
        ) -> None:
            nonlocal injected
            # 401 已发生后的下一次 Task 刷新 = _fail_planning_inplace 的 guard 刷新
            # → 注入并发 FAILED（另一连接）
            if not injected and isinstance(instance, Task) and chatted:
                injected = True
                with session_factory() as cancel_session:
                    task_row = cancel_session.get(Task, task_id)
                    assert task_row is not None
                    task_row.status = "FAILED"
                    task_row.ended_at = _NOW
                    task_row.updated_at = _NOW
                    cancel_session.commit()
            original_refresh(instance, attribute_names, with_for_update)

        session.refresh = refresh_with_cancel  # type: ignore[method-assign]
        _claim_and_plan(session, client=_client_with_handler(handler))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        attempts = session.scalars(
            select(LlmCallAttempt).where(LlmCallAttempt.task_id == task_id)
        ).all()
    assert task.status == "FAILED"  # 并发终态不被 guard 的 FAILED 覆盖
    assert [a.status for a in attempts] == ["FAILED"]  # 账本 401 失败已记（预算消耗）


def test_planning_fingerprint_drift_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """review fix 3（§6.2）：账本 fingerprint 与重推导不一致 → 输入漂移失败
    （FAILED + PLANNING + 兜底错误码），fail fast 不发调用、不复用旧结果。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, _ = _seed_planning_task(session, user_id=user)
        op_key = f"planning:coarse:{chapter_id}:0"
        # 用错误 fingerprint 预置一次 SUCCESS（模拟规划输入漂移：分段/区间/版本变化）
        attempt = create_attempt(
            session,
            user_id=user,
            scope_type="TASK",
            scope_id=task_id,
            task_id=task_id,
            stage="PLANNING",
            operation_key=op_key,
            input_fingerprint="stale-fingerprint",
            attempt_no=1,
            model="m",
            prompt_name="planner-coarse",
            prompt_version="v7",
            now=_NOW,
        )
        finish_success(
            session,
            attempt,
            usage={
                "prompt_cache_hit_tokens": 0,
                "prompt_cache_miss_tokens": 1,
                "completion_tokens": 1,
            },
            http_status=200,
            duration_ms=1,
            normalized_result="[]",
            now=_NOW,
        )
        session.commit()
        state: dict[str, int] = {"calls": 0}
        _claim_and_plan(session, client=_client_with_handler(_two_stage_handler(state)))
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 0  # 漂移失败不发任何调用
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.error_code == "GENERATION_FAILED"


def test_planning_mixed_skipped_and_empty_records_skips(
    session_factory: Callable[[], Session],
) -> None:
    """review fix 4（§6.4）：部分粗规划段跳过 + 其余成功但 0 主题 → COMPLETED
    NO_GENERATION_UNITS 且 skipped_planning_group_count 保留观测。"""
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_coarse_max_input_chars=1600,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(
            session, user_id=user, chapter_start_page=1, chapter_end_page=4
        )
        pages = _load_pages(session, file_id)
        assert len(pages) == 4
        # 段 0（页 1-2，1500 字）预算耗尽 → SKIPPED；段 1（页 3-4）成功返回 0 主题
        seg_interval = interval_for_chapter(
            sum(p.char_count for p in pages[:2]), "COMPACT", _anchors(_SETTINGS)
        )
        fp0 = coarse_fingerprint(pages[:2], seg_interval, "COMPACT", asset_versions())
        op_key0 = f"planning:coarse:{chapter_id}:0"
        for attempt_no in (1, 2, 3):
            att = create_attempt(
                session,
                user_id=user,
                scope_type="TASK",
                scope_id=task_id,
                task_id=task_id,
                stage="PLANNING",
                operation_key=op_key0,
                input_fingerprint=fp0,
                attempt_no=attempt_no,
                model="m",
                prompt_name="planner-coarse",
                prompt_version="v7",
                now=_NOW,
            )
            att.status = ("STARTED", "FAILED", "UNKNOWN")[attempt_no - 1]
            att.finished_at = _NOW
        session.commit()
        state: dict[str, int] = {"calls": 0}
        handler = _two_stage_handler(state, coarse=lambda p: [])
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=_client_with_handler(handler))
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert state["calls"] == 1  # 段 0 预算耗尽跳过；仅段 1 调用（成功空结果）
    assert task.status == "COMPLETED"
    assert task.completion_reason == "NO_GENERATION_UNITS"
    assert task.skipped_planning_group_count == 1  # 部分跳过观测不丢
    assert task.total_batch_count == 0
    assert task.completed_batch_count == 0


def test_planning_legacy_task_no_user_fails_clean(
    session_factory: Callable[[], Session],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3 Minor ①：legacy 任务（user_id NULL 的历史行）→ run_planning 干净 FAILED
    （PLANNING_TASK_INCOMPLETE 内部原因入日志、error_code 兜底 GENERATION_FAILED），
    不 500、不发 LLM 调用、不产生无主账本行。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        task = session.get(Task, task_id)
        assert task is not None
        task.user_id = None  # 直插模拟 user_id 缺失的历史行（SQLAlchemy 允许，无需其他表）
        session.commit()
    with session_factory() as session:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise AssertionError("legacy 任务不得发 LLM 调用")

        # alembic env.py fileConfig(disable_existing_loggers) 会禁用未配置 logger
        # （先跑过迁移测试时生效）——显式重新启用，保证 caplog 捕获稳定
        monkeypatch.setattr(
            logging.getLogger("services.generation.planning_executor"), "disabled", False
        )
        with caplog.at_level(logging.WARNING):
            _claim_and_plan(session, client=_client_with_handler(handler))
    assert calls == 0  # guard 在 create_attempt/chat 前
    with session_factory() as session:
        task = session.get(Task, task_id)
        attempts = session.scalars(select(LlmCallAttempt)).all()
    assert task is not None
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.error_code == "GENERATION_FAILED"
    assert attempts == []  # 无无主账本行
    reasons = [
        getattr(r, "internal_reason", None)
        for r in caplog.records
        if getattr(r, "message", "") == "task planning failed"
    ]
    assert "PLANNING_TASK_INCOMPLETE" in reasons
