"""规划执行集成测试（spec §6.1/§6.2/§6.4；Task 9）：CAS 抢占/快照冻结/分组调用/账本恢复/合并落库。

基座同 test_tasks_executor.py：真实 SQLite 全表建库 + mock transport client
（brief 提及的 session/settings_override fixture 仓库不存在，按仓库约定用
session_factory 定式——adaptation 见任务报告）。mock chat 从请求体提取当前组页
（<PLANNER_INPUT> 内的 source_chunks），保证回复引用合法来源。
"""

import json
import logging
import sys
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
    group_fingerprint,
    run_planning,
)
from services.generation.quota import (
    allocate_chapter_intervals,
    allocate_group_interval,
    difficulty_interval,
    interval_for_chapter,
)
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
    """确定性页文本（chunk_id 由 (file_id, page, content) 决定，测试可复算）。"""
    return f"第{page_number}页内容" * 20


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


def _planning_response_from_request(
    request: httpx.Request, *, count: int = 1, difficulties: list[str] | None = None
) -> str:
    """从请求 user message 的 <PLANNER_INPUT> 提取当前组页 → 合法单元响应。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0])
    chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
    diffs = difficulties or ["BASIC"]
    units = [
        {
            "source_chunk_ids": [chunk_ids[i % len(chunk_ids)]],
            "learning_objective": f"目标{i}",
            "target_difficulty": diffs[i % len(diffs)],
            "card_type": "QUESTION",
            "coverage_tier": "CORE",
        }
        for i in range(count)
    ]
    return json.dumps({"units": units}, ensure_ascii=False)


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


def _expected_sub_interval(pages: list[Any]) -> dict[str, dict[str, int]]:
    """镜像 run_planning 的密度区间链（V25-D-25）：1 章 COMPACT 40/40/20 → 组区间。

    测试页字符总量 < planner_max_input_chars → 单组，组区间 = 章难度区间。
    """
    from app.config import Settings

    settings = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
    char_counts = [sum(p.char_count for p in pages)]
    intervals = allocate_chapter_intervals(
        char_counts,
        "COMPACT",
        0.4,
        0.4,
        0.2,
        cards_per_10k={
            "COMPACT": settings.cards_per_10k_compact,
            "BALANCED": settings.cards_per_10k_balanced,
            "EXTENSIVE": settings.cards_per_10k_extensive,
        },
    )
    return intervals[0]


def _claim_and_plan(
    session: Session, *, settings: Settings = _SETTINGS, client: DeepSeekClient
) -> Task:
    task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
    assert task is not None
    session.commit()
    run_planning(session, task, settings=settings, client=client)
    session.commit()
    return task


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
            operation_key=f"planning:{chapter_id}:0",
            input_fingerprint="fp-stale",
            attempt_no=1,
            model="m",
            prompt_name="planner",
            prompt_version="v3",
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


# ---------- 规划执行 ----------


def test_planning_success_units_and_batches(
    session_factory: Callable[[], Session],
) -> None:
    """成功规划 → GENERATING + KnowledgePoint（含新列/兼容投影）+ 每单元一批（generation_unit_id）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(
                _planning_response_from_request(
                    request, count=2, difficulties=["BASIC", "UNDERSTANDING"]
                )
            )

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
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
    assert calls == 1
    assert task.stage == "GENERATING"
    assert task.status == "GENERATING"  # V2.5 全程 GENERATING（规划完成不转移用户状态）
    assert task.skipped_planning_group_count == 0
    assert len(kps) == 2  # 每单元一个知识点（BASIC + UNDERSTANDING 各 1，配额内）
    assert [kp.target_difficulty for kp in kps] == ["BASIC", "UNDERSTANDING"]
    assert all(kp.card_type == "QUESTION" for kp in kps)
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


def test_planning_success_reuses_normalized(
    session_factory: Callable[[], Session],
) -> None:
    """账本已有同 operation_key+fingerprint 的 SUCCESS → 复用 normalized_result，0 次调用。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(session, user_id=user)
    # 计算 run_planning 会使用的 operation_key/fingerprint/子配额（镜像实现）
    with session_factory() as session:
        from infra.db.models import TextChunk

        pages = list(
            session.scalars(
                select(TextChunk)
                .where(TextChunk.file_id == file_id)
                .order_by(TextChunk.page_number)
            ).all()
        )
        interval = _expected_sub_interval(pages)
        op_key = f"planning:{chapter_id}:0"
        fp = group_fingerprint(pages, interval, asset_versions(), "COMPACT")
        units = [
            {
                "source_chunk_ids": [pages[0].chunk_id],
                "learning_objective": "复用目标",
                "target_difficulty": "BASIC",
                "card_type": "QUESTION",
                "priority": 1,
            }
        ]
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
            prompt_version="v3",
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
            normalized_result=json.dumps(units, ensure_ascii=False),
            now=_NOW,
        )
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert calls == 0  # find_success_result 命中复用，不重复付费调用
    assert task.stage == "GENERATING"
    assert len(kps) == 1
    assert kps[0].topic == "复用目标"


def test_planning_budget_reset_prevented(
    session_factory: Callable[[], Session],
) -> None:
    """账本已有 3 次尝试（预算耗尽）→ 组 SKIPPED、0 次调用、skipped 计数（预算不重置）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(session, user_id=user)
        from infra.db.models import TextChunk

        pages = list(
            session.scalars(
                select(TextChunk)
                .where(TextChunk.file_id == file_id)
                .order_by(TextChunk.page_number)
            ).all()
        )
        interval = _expected_sub_interval(pages)
        op_key = f"planning:{chapter_id}:0"
        fp = group_fingerprint(pages, interval, asset_versions(), "COMPACT")
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
                prompt_name="planner",
                prompt_version="v3",
                now=_NOW,
            )
            # STARTED/FAILED/UNKNOWN 任意组合均计入预算（spec §9）
            att.status = ("STARTED", "FAILED", "UNKNOWN")[attempt_no - 1]
            att.finished_at = _NOW
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert calls == 0
    assert task.status == "FAILED"  # 全部组 SKIPPED → §6.4 分支 2
    assert task.failure_stage == "PLANNING"
    assert task.skipped_planning_group_count == 1


def test_planning_empty_units_completed_no_units(
    session_factory: Callable[[], Session],
) -> None:
    """全组成功但 0 个合法单元 → COMPLETED + NO_GENERATION_UNITS（§6.4 分支 1）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        client = _client_with_handler(lambda request: _ok_response('{"units": []}'))
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert task.status == "COMPLETED"
    assert task.completion_reason == "NO_GENERATION_UNITS"
    assert task.total_batch_count == 0
    assert task.completed_batch_count == 0
    assert task.generated_card_count == 0
    assert task.resumable == 0


def test_planning_all_failed_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """上游持续失败（retryable）→ 3 次尝试后组 SKIPPED、全组 SKIPPED → FAILED+PLANNING。"""
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
    """全部组成功后在最终事务前并发 FAILED（另一 worker 系统级失败）→ 条件更新
    rowcount=0 → 不写 KnowledgePoint/Batch（V2.5 无 cancel，终态守卫以 FAILED 等价验证）。"""
    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        calls = 0
        injected = False
        original_refresh = session.refresh

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        def refresh_with_cancel(
            instance: object,
            attribute_names: Any = None,
            with_for_update: Any = None,
        ) -> None:
            nonlocal injected
            # 最终短事务前的 Task 刷新（chat 已完成后）→ 注入并发 FAILED（另一连接）
            if not injected and isinstance(instance, Task) and calls >= 1:
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
        kp_count = session.scalar(
            select(func.count())
            .select_from(KnowledgePoint)
            .where(KnowledgePoint.task_id == task_id)
        )
        batch_count = session.scalar(
            select(func.count()).select_from(Batch).where(Batch.task_id == task_id)
        )
    assert calls == 1
    assert task.status == "FAILED"  # 并发终态不被最终事务覆盖
    assert kp_count == 0  # 条件不成立 → 整事务回滚
    assert batch_count == 0


def test_planning_groups_split_and_sub_quota(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """按 planner_max_input_chars 连续页拆组：2 组各一次调用、每组只引用本组页。

    密度制（V25-D-25）：页文本加厚到 300 字符（总 1200，EXTENSIVE 目标 2.4 张），
    保证两组的难度区间上界都 ≥1——薄内容在密度制下合法地产出更少单元。
    """
    monkeypatch.setattr(sys.modules[__name__], "_page_content", lambda pn: f"第{pn}页内容" * 120)
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_max_input_chars=1300,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        # EXTENSIVE 预算 9：最大余数法后两组子配额均非零（COMPACT 预算 3 会被
        # 最大余数法全部分给 300 字符大组，小组零配额）
        task_id, _, _ = _seed_planning_task(
            session,
            user_id=user,
            chapter_start_page=1,
            chapter_end_page=4,
            coverage_mode="EXTENSIVE",
        )
        received: list[list[str]] = []
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            body = json.loads(request.content)
            user = body["messages"][-1]["content"]
            payload = json.loads(
                user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0]
            )
            received.append([c["chunk_id"] for c in payload["source_chunks"]])
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=client)
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert calls == 2  # 4 页 × 600 字符 = 2400 > 1300 → 2 组
    assert [len(pages) for pages in received] == [2, 2]  # 连续页分组：[1,2] + [3,4]
    assert all(received[0][i] != received[1][0] for i in range(2))  # 组间无重叠页
    assert len(kps) == 2
    assert task.total_batch_count == 2


def test_planning_hard_cap_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """组数 > max_planner_groups_per_task → 任务 FAILED + PLANNING（不发调用，§6.3 硬上限）。"""
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_max_input_chars=100,
        max_planner_groups_per_task=1,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(
            session, user_id=user, chapter_start_page=1, chapter_end_page=4
        )
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=client)
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert calls == 0  # 硬上限失败不发 Planner 请求
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
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert calls == 0
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
    steps = iter(FrozenClock(base + timedelta(minutes=2 * i)) for i in range(1, 20))
    monkeypatch.setattr(planning_mod, "SystemClock", lambda: next(steps))

    user = _uuid()
    with session_factory() as session:
        task_id, _, _ = _seed_planning_task(session, user_id=user)
        client = _client_with_handler(
            lambda request: _ok_response(_planning_response_from_request(request))
        )
        _claim_and_plan(session, client=client)
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
    → rowcount=0 → guard 的 FAILED 不覆盖并发终态（V2.5 无 cancel，以 FAILED 等价验证）。"""
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
        op_key = f"planning:{chapter_id}:0"
        # 用错误 fingerprint 预置一次 SUCCESS（模拟规划输入漂移：分组/配额/版本变化）
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
            prompt_name="planner",
            prompt_version="v3",
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
            normalized_result='{"units": []}',
            now=_NOW,
        )
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response(_planning_response_from_request(request))

        client = _client_with_handler(handler)
        _claim_and_plan(session, client=client)
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert calls == 0  # 漂移失败不发任何调用
    assert task.status == "FAILED"
    assert task.failure_stage == "PLANNING"
    assert task.error_code == "GENERATION_FAILED"


def test_planning_mixed_skipped_and_empty_records_skips(
    session_factory: Callable[[], Session],
) -> None:
    """review fix 4（§6.4）：部分组跳过 + 其余成功但 0 单元 → COMPLETED
    NO_GENERATION_UNITS 且 skipped_planning_group_count 保留观测。"""
    user = _uuid()
    settings = Settings(
        api_key_encryption_key="aa" * 32,
        planner_max_input_chars=300,
        _env_file=None,  # type: ignore[call-arg]
    )
    with session_factory() as session:
        task_id, chapter_id, file_id = _seed_planning_task(
            session, user_id=user, chapter_start_page=1, chapter_end_page=4
        )
        from infra.db.models import TextChunk

        pages = list(
            session.scalars(
                select(TextChunk)
                .where(TextChunk.file_id == file_id)
                .order_by(TextChunk.page_number)
            ).all()
        )
        # 镜像难度区间（V25-D-25）：组 0（页 1-3）的区间由章区间按字符占比拆分
        from app.config import Settings as _Settings

        _s = _Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
        chapter_interval = interval_for_chapter(
            400,
            "COMPACT",
            cards_per_10k={
                "COMPACT": _s.cards_per_10k_compact,
                "BALANCED": _s.cards_per_10k_balanced,
                "EXTENSIVE": _s.cards_per_10k_extensive,
            },
        )
        di = difficulty_interval(chapter_interval, 0.4, 0.4, 0.2)
        sub_interval = allocate_group_interval(di, [300, 100])[0]
        op_key0 = f"planning:{chapter_id}:0"
        fp0 = group_fingerprint(pages[:3], sub_interval, asset_versions(), "COMPACT")
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
                prompt_name="planner",
                prompt_version="v3",
                now=_NOW,
            )
            att.status = ("STARTED", "FAILED", "UNKNOWN")[attempt_no - 1]
            att.finished_at = _NOW
        session.commit()
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _ok_response('{"units": []}')

        client = _client_with_handler(handler)
        task = claim_planning_task(session, orphan_timeout_minutes=30, now=_CLAIM_NOW)
        assert task is not None
        session.commit()
        run_planning(session, task, settings=settings, client=client)
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
    assert calls == 1  # 组 0 预算耗尽跳过；仅组 1 调用（成功空结果）
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
