"""V5B 并发/心跳集成测试：批次抢占单执行者/心跳刷新（真实 SQLite + mock transport）。

种子写入真实加密 Key（executor 解密路径）；process_active_tasks 注入
settings + client_factory（mock transport），生产缺省路径不在此验证。

Task 6 追加（structure-contract 4.1 并发/发布语义）：重复确认、worker 崩溃恢复
单效果、重试后发布完整替代结果、任务删除清理 STAGED 残留、0 张有效卡整体失败；
LLM 调用不得持有 SQLite 写事务（§9 R-17）在 transport handler 内断言。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Batch, Card, KnowledgePoint, Task, TextChunk, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches, process_next_batch
from services.generation.samples import config_fingerprint
from services.generation.scoring import enter_scoring_stage, run_scoring_stage
from services.tasks.executor import _fail_task, process_active_tasks
from services.tasks.service import delete_task, retry_task, start_task

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(
    session: Session,
    *,
    user_id: str,
    coverage_mode: str = "COMPACT",
    n_units: int | None = None,
) -> str:
    """种子：GENERATING 任务（stage=GENERATING）+ 页文本 + 生成单元（锚定难度/卡型/来源页）+
    按单元建批（spec §7 批=单元，generation_unit_id 必填）。

    T8 起 create_task 不再规划知识点（PENDING+PLANNING）；V5B 并发/心跳测试聚焦
    生成路径，直接构造单元与批次绕过规划 worker（规划路径由 test_planning_executor.py
    覆盖）。test 1 直接调 process_next_batch，不经过 executor 的 plan 路径。
    n_units 覆盖单元数（test 1 单批次抢占场景用 1 单元 = 1 批）。
    """
    from infra.db.models import ApiKey, Chapter, LearningProject, PdfFile
    from services.decks.service import create_deck
    from services.pdf.text_chunks import persist_text_chunks
    from services.tasks.service import create_task

    # FK 前置守卫：users 行必须先存在（engine 级 PRAGMA foreign_keys=ON）
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-08-10T00:00:00.000Z",
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="b.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-10T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at="2026-08-10T00:00:00.000Z",
        version="2026-08-10T00:00:00.000Z",
        created_at="2026-08-10T00:00:00.000Z",
        updated_at="2026-08-10T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-10T00:00:00.000Z")
    deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
    session.flush()
    ch = Chapter(chapter_id=_uuid(), file_id=pdf.file_id, name="第一章", start_page=1, end_page=2)
    session.add(ch)
    session.flush()
    if (
        session.scalar(
            select(ApiKey.user_id).where(ApiKey.user_id == user_id, ApiKey.status == "AVAILABLE")
        )
        is None
    ):
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now="2026-08-10T00:00:00.000Z",
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
        now="2026-08-10T00:00:00.000Z",
    )
    task.status = "GENERATING"  # V2.5 七态：跳过样卡阶段直入生成（并发/心跳聚焦）
    task.stage = "GENERATING"
    task.updated_at = "2026-08-10T00:00:00.000Z"
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    diffs = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]  # V2.5 改名（3.5）
    n_kps = n_units if n_units is not None else {"COMPACT": 3, "BALANCED": 6}.get(coverage_mode, 3)
    kps = [
        KnowledgePoint(
            knowledge_point_id=str(uuid.uuid4()),
            task_id=task.task_id,
            chapter_id=ch.chapter_id,
            source_chunk_id=chunks[0].chunk_id,  # 兼容投影（spec §3.1）
            topic=f"知识点{i + 1}",
            priority=i + 1,
            status="PENDING",
            target_difficulty=diffs[i % len(diffs)],
            card_type="QUESTION",
            source_chunk_ids=json.dumps([c.chunk_id for c in chunks], ensure_ascii=False),
        )
        for i in range(n_kps)
    ]
    session.add_all(kps)
    session.flush()
    plan_batches(
        session, task_id=task.task_id, generation_units=kps, now="2026-08-10T00:00:00.000Z"
    )
    session.commit()
    return task.task_id


def _valid_cards_json(n: int = 1) -> str:
    """每批 1 张合法卡（spec §7：批=单元，generator-output schema v2 maxItems=1）。"""
    cards = [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]
    return json.dumps({"cards": cards}, ensure_ascii=False)


def _scoring_content(request: httpx.Request) -> str:
    """<SCORING_INPUT> 提取 items → ID 守恒的确定性分数（总分代码计算 9）。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0])
    return json.dumps(
        {
            "scores": [
                {
                    "generation_item_id": item["generation_item_id"],
                    "evidence_score": 2,
                    "correctness_score": 3,
                    "difficulty_score": 2,
                    "learning_value_score": 2,
                }
                for item in payload["items"]
            ]
        },
        ensure_ascii=False,
    )


def _client() -> DeepSeekClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        content = _scoring_content(request) if "<SCORING_INPUT>" in user else _valid_cards_json()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def _client_factory(api_key: str) -> DeepSeekClient:
    return _client()


def test_concurrency_two_workers_single_effect(session_factory: Callable[[], Session]) -> None:
    """两 worker 并发处理同任务：批次条件更新抢占 → 单执行者（无双处理）。

    1 单元 = 1 批（spec §7）：worker A 抢占唯一批次并完成后，worker B 无
    PENDING/FAILED 批次可取 → 0（旧 batch_size 语义下 3 知识点 = 1 批同构）。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)
    client = _client()
    with session_factory() as session:
        # worker A 取批次（PENDING→PROCESSING）
        n1 = process_next_batch(session, task_id=task_id, client=client)
        session.commit()
        # worker B 再取（同一批次已 PROCESSING → 不可取；取下一个或 0）
        n2 = process_next_batch(session, task_id=task_id, client=client)
        session.commit()
    assert n1 == 1
    assert n2 == 0  # 同批次被 A 抢占，B 无批次可取


def test_concurrency_heartbeat_updates_updated_at(session_factory: Callable[[], Session]) -> None:
    """心跳：每批完成后 task.updated_at 刷新（seed 时间取安全过去值——与真实服务端时钟可观测比较）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)
        seeded = session.get(Task, task_id)
        assert seeded is not None
        created_at = seeded.created_at
    with session_factory() as session:
        process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    with session_factory() as session:
        task = session.get(Task, task_id)
    assert task is not None
    assert task.updated_at is not None and created_at is not None
    assert task.updated_at > created_at  # 心跳刷新（批处理后时间推进）
    assert task.status == "COMPLETED"


def test_concurrency_batch_commit_survives_crash(
    session_factory: Callable[[], Session],
) -> None:
    """批次事务粒度：批 2 处理中崩溃（SystemExit）→ 批 1 已落库（卡+心跳+SUCCEEDED）。

    崩溃模拟：mock transport 第 2 次 chat 抛 SystemExit（BaseException——绕过 executor 的
    except Exception）→ 批 2 的 claim/STARTED 已随调用前事务提交（spec §9），批次保持
    PROCESSING、任务保持 RUNNING；批 1 的批次状态+游标+心跳已随批次事务提交
    （与单次最终 commit 的差异点——Task 3 恢复语义：已完成批次保留、未完成批次经
    心跳超时孤儿恢复后重试）。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, coverage_mode="BALANCED")
    with session_factory() as session:
        seeded = session.get(Task, task_id)
        assert seeded is not None
        created_at = seeded.created_at
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit("模拟崩溃：批 2 处理中断")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    crashed = False
    with session_factory() as session:
        try:
            process_active_tasks(session, settings=_SETTINGS, client_factory=lambda _k: client)
        except SystemExit:
            crashed = True
            session.rollback()  # 崩溃连接释放写锁（等价于进程死亡）
    assert crashed
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None
        batches = session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert calls == 2  # 批 1 成功、批 2 崩溃
    assert task.status == "GENERATING"  # V2.5 七态：终态未落库（崩溃发生在 COMPLETED 之前）
    assert task.updated_at is not None and created_at is not None
    assert task.updated_at > created_at  # 批 1 心跳已随批提交
    # 批 1 已提交；批 2 抢占+STARTED 已提交（spec §9 调用前占位）→ PROCESSING 保留
    # （恢复语义：心跳超时孤儿恢复将其复位 FAILED 后按账本重试——test_ledger 同款）
    assert [b.status for b in batches] == [
        "SUCCEEDED",
        "PROCESSING",
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
    ]
    assert len(cards) == 1  # 批 1 卡片已提交（崩溃不丢已完成批次）


# ---------- Task 6：并发/发布语义（structure-contract 4.1） ----------


def _planning_content(request: httpx.Request) -> str:
    """<PLANNER_INPUT> 提取组页 → 2 个合法单元（引用请求内首页；2 单元 = 2 批，
    供"批 2 失败注入"与替代结果断言使用）。"""
    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    payload = json.loads(user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0])
    chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
    return json.dumps(
        {
            "units": [
                {
                    "source_chunk_ids": [chunk_ids[0]],
                    "learning_objective": "规划目标一",
                    "target_difficulty": "BASIC",
                    "card_type": "QUESTION",
                    "coverage_tier": "CORE",
                },
                {
                    "source_chunk_ids": [chunk_ids[0]],
                    "learning_objective": "规划目标二",
                    "target_difficulty": "UNDERSTANDING",
                    "card_type": "QUESTION",
                    "coverage_tier": "CORE",
                },
            ]
        },
        ensure_ascii=False,
    )


def _seed_planning_task(session: Session, *, user_id: str) -> str:
    """GENERATING+PLANNING 任务（start 后状态）+ 章节 + 页文本（text_chunks）：
    规划 worker 全流程基座（重试替代结果用例）。"""
    from infra.db.models import ApiKey, Chapter, LearningProject, PdfFile
    from services.decks.service import create_deck
    from services.pdf.text_chunks import persist_text_chunks
    from services.tasks.service import create_task

    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-08-10T00:00:00.000Z",
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="p.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-10T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at="2026-08-10T00:00:00.000Z",
        version="2026-08-10T00:00:00.000Z",
        created_at="2026-08-10T00:00:00.000Z",
        updated_at="2026-08-10T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-10T00:00:00.000Z")
    deck.project_id = project.project_id
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
                updated_at="2026-08-10T00:00:00.000Z",
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now="2026-08-10T00:00:00.000Z",
    )
    task = create_task(
        session,
        user_id=user_id,
        project_id=project.project_id,
        deck_id=deck.deck_id,
        chapter_ids=[ch.chapter_id],
        config=GenerationConfig(
            coverage_mode="COMPACT",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        ),
        now="2026-08-10T00:00:00.000Z",
    )
    task.status = "GENERATING"
    task.stage = "PLANNING"
    task.updated_at = "2026-08-10T00:00:00.000Z"
    session.commit()
    return task.task_id


def test_concurrency_duplicate_confirmation_rejected(
    session_factory: Callable[[], Session],
) -> None:
    """重复确认（4.1 start 校验）：同一任务第二次 start → TASK_STATE_CONFLICT
    （CAS 条件更新，并发确认不互相覆盖；与幂等键防重复触发互不替代）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)
        task = session.get(Task, task_id)
        assert task is not None
        # 直写 AWAITING + 有效样卡（配置指纹一致）→ 可 start
        task.status = "AWAITING_SAMPLE_CONFIRMATION"
        task.sample_cards = json.dumps(
            [{"card_id": _uuid(), "front": "f", "back": "b", "card_type": "QUESTION"}],
            ensure_ascii=False,
        )
        task.sample_config_hash = config_fingerprint(
            GenerationConfig(
                coverage_mode="COMPACT",
                difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
            )
        )
        task.sample_confirmed_at = "2026-08-10T00:00:00.000Z"
        session.commit()
    with session_factory() as session:
        started = start_task(session, user_id=user, task_id=task_id, now="2026-08-10T00:00:00.000Z")
        session.commit()
        assert started.status == "GENERATING"
    with session_factory() as session, pytest.raises(AppError) as excinfo:
        start_task(session, user_id=user, task_id=task_id, now="2026-08-10T00:00:00.000Z")
    assert excinfo.value.code is ErrorCode.TASK_STATE_CONFLICT
    # 重复确认后任务仍保持首次确认结果（不复活不覆盖）
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None and row.status == "GENERATING" and row.stage == "PLANNING"


def test_concurrency_llm_call_holds_no_write_transaction(
    session_factory: Callable[[], Session],
) -> None:
    """§9 R-17 硬规则：LLM 调用不得持有 SQLite 写事务——transport handler 内断言
    session 无打开事务（调用前 STARTED 占位已独立提交，chat 时无写锁）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)

    def handler(request: httpx.Request) -> httpx.Response:
        assert not session.in_transaction(), "LLM 调用期间持有 SQLite 写事务"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    with session_factory() as session:
        n = process_active_tasks(
            session,
            settings=_SETTINGS,
            client_factory=lambda _k: DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(handler)
            ),
        )
        session.commit()
    assert n == 1
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.status == "COMPLETED"


def test_concurrency_worker_crash_recovery_single_effect(
    session_factory: Callable[[], Session],
) -> None:
    """worker 崩溃 → 孤儿恢复：批 1 已提交卡 STAGED 隔离（并发中间态零可见）；
    心跳超时后下一轮扫描接管续跑 → COMPLETED 且无重复卡（账本 UNKNOWN 计预算 +
    防重先查后插双保险）。LLM 调用期间不得持有写事务（handler 内断言）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=3)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert not session.in_transaction(), "LLM 调用期间持有 SQLite 写事务"
        if calls == 2:
            raise SystemExit("模拟崩溃：批 2 处理中断")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    crashed = False
    with session_factory() as session:
        try:
            process_active_tasks(
                session,
                settings=_SETTINGS,
                client_factory=lambda _k: DeepSeekClient(
                    _SETTINGS, transport=httpx.MockTransport(handler)
                ),
            )
        except SystemExit:
            crashed = True
            session.rollback()
    assert crashed
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        assert task.status == "GENERATING"
        assert len(cards) == 1 and cards[0].publication_state == "STAGED"  # 批 1 隔离
    # 孤儿窗口流逝 → 恢复扫描（新 worker 接管）→ 单效果：无重复入库
    with session_factory() as session:
        task_row = session.get(Task, task_id)
        assert task_row is not None
        task_row.updated_at = "2026-07-01T00:00:00.000Z"
        session.commit()
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert task.generated_card_count == 3
    assert len(cards) == 3
    assert len({c.generation_item_id for c in cards}) == 3  # 无重复卡
    assert all(c.publication_state == "PUBLISHED" for c in cards)  # 恢复后原子发布


def test_concurrency_publishing_orphan_recovered(
    session_factory: Callable[[], Session],
) -> None:
    """PUBLISHING 孤儿恢复：worker 崩溃于 SCORING→PUBLISHING 提交之后、发布之前
    → 下一轮扫描（GENERATING+PUBLISHING + 心跳超时）CAS 接管 → 直接发布（无 LLM）
    → COMPLETED。发布条件更新幂等：不重复发布。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)
    client = _client()
    with session_factory() as session:
        # 手动执行到崩溃点：批生成完成 → SCORING 阶段完成（终态 PUBLISHING 已提交）
        while process_next_batch(session, task_id=task_id, client=client) > 0:
            session.commit()
        task = session.get(Task, task_id)
        assert task is not None
        assert enter_scoring_stage(session, task_id=task_id, settings=_SETTINGS)
        session.commit()
        run_scoring_stage(session, task=task, settings=_SETTINGS, client=client)
        session.commit()  # 模拟崩溃于 PUBLISHING 提交后、发布前
        task_row = session.get(Task, task_id)
        assert task_row is not None
        assert task_row.status == "GENERATING" and task_row.stage == "PUBLISHING"
        task_row.updated_at = "2026-07-01T00:00:00.000Z"  # 心跳超时（在途 worker 崩溃）
        session.commit()
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1  # 评分 worker 接管 PUBLISHING 孤儿（无 LLM 调用）
    assert task.status == "COMPLETED"
    assert task.generated_card_count == 1
    assert [c.publication_state for c in cards] == ["PUBLISHED"]
    # 幂等：COMPLETED 后再次扫描不再发布/复活
    with session_factory() as session:
        n2 = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    assert n2 == 0
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.status == "COMPLETED"
        assert task.generated_card_count == 1


def test_concurrency_retry_after_failure_publishes_replacement(
    session_factory: Callable[[], Session],
) -> None:
    """失败重试 → 完整替代结果（4.1/5.13）：原任务生成中途 401 FAILED（已入库卡
    STAGED 隔离、generated_card_count=0）；retry 创建关联新任务（沿用已确认样卡）
    → 重新规划+生成+评分 → 原子发布 COMPLETED。原失败任务保留，其 STAGED 卡
    继续隔离；用户牌组列表只呈现替代任务的完整结果（零部分可见）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_planning_task(session, user_id=user)
        task_row = session.get(Task, task_id)
        assert task_row is not None and task_row.deck_id is not None
    run_phase = {"gen_calls": 0, "fail_run": True}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "<SCORING_INPUT>" in user_msg:
            content = _scoring_content(request)
        elif "<PLANNER_INPUT>" in user_msg:
            content = _planning_content(request)
        else:  # GENERATOR_INPUT
            run_phase["gen_calls"] += 1
            if run_phase["fail_run"] and run_phase["gen_calls"] == 2:
                return httpx.Response(401, json={"error": {"message": "invalid api key"}})
            content = _valid_cards_json()
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    def factory(_api_key: str) -> DeepSeekClient:
        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    # 第一轮：规划成功 → 批 1 成功 → 批 2 Key 失效 → FAILED（STAGED 1 张隔离）
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=factory)
        session.commit()
        original = session.get(Task, task_id)
        assert original is not None and original.deck_id is not None
        staged = session.scalars(select(Card).where(Card.deck_id == original.deck_id)).all()
        original_task_id = task_id
    assert n == 1
    assert original.status == "FAILED"
    assert original.error_code == "API_KEY_UNAVAILABLE"
    assert original.generated_card_count == 0  # 失败任务为 0（只统计已发布卡）
    assert len(staged) == 1 and staged[0].publication_state == "STAGED"
    # 直写已确认样卡（正式生成失败可沿用）→ retry 新任务 AWAITING 可直接 start
    with session_factory() as session:
        task_row = session.get(Task, original_task_id)
        assert task_row is not None
        task_row.sample_cards = json.dumps(
            [{"card_id": _uuid(), "front": "f", "back": "b", "card_type": "QUESTION"}],
            ensure_ascii=False,
        )
        task_row.sample_config_hash = config_fingerprint(
            GenerationConfig(
                coverage_mode="COMPACT",
                difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
            )
        )
        task_row.sample_confirmed_at = "2026-08-10T00:00:00.000Z"
        session.commit()
    with session_factory() as session:
        new_task = retry_task(
            session, user_id=user, task_id=original_task_id, now="2026-08-10T00:00:00.000Z"
        )
        session.commit()
        assert new_task.retry_of_task_id == original_task_id
        assert new_task.status == "AWAITING_SAMPLE_CONFIRMATION"
        new_task_id = new_task.task_id
        started = start_task(
            session, user_id=user, task_id=new_task_id, now="2026-08-10T00:00:00.000Z"
        )
        session.commit()
        assert started.status == "GENERATING" and started.stage == "PLANNING"
    # 第二轮：替代任务全流程（规划→生成→评分→原子发布）→ COMPLETED
    run_phase["fail_run"] = False  # 替代任务不再注入失败
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=factory)
        session.commit()
        replacement = session.get(Task, new_task_id)
        original_row = session.get(Task, original_task_id)
        assert replacement is not None and replacement.deck_id is not None
        assert original_row is not None and original_row.deck_id is not None
        cards = session.scalars(
            select(Card).where(Card.deck_id == replacement.deck_id).order_by(Card.position)
        ).all()
    assert n == 1
    assert replacement.status == "COMPLETED"
    assert replacement.generated_card_count == 2  # 发布一个完整替代结果（2 批 2 卡）
    assert original_row.status == "FAILED"  # 原失败任务保留（5.13）
    assert [c.publication_state for c in cards] == ["STAGED", "PUBLISHED", "PUBLISHED"]
    assert cards[0].source_task_id == original_task_id  # 原任务 STAGED 卡继续隔离
    assert cards[1].source_task_id == new_task_id and cards[2].source_task_id == new_task_id
    # 用户侧：只呈现替代任务的完整结果（零部分可见）
    from services.cards.service import list_cards

    with session_factory() as session:
        visible = list_cards(session, user_id=user, deck_id=str(replacement.deck_id))
    assert [c.card_id for c in visible] == [cards[1].card_id, cards[2].card_id]


def test_concurrency_task_delete_cleans_staged_residuals(
    session_factory: Callable[[], Session],
) -> None:
    """任务删除清理（4.1 删除规则）：失败任务遗留 STAGED 卡级联清理——绝不转为
    无来源可见卡；delete_generated_cards 只决定已发布卡去留（保留 → source_task_id
    置空；删除 → 连卡删除）。"""
    user = _uuid()
    # FAILED 任务 + 1 张 STAGED 残留（批 2 401）
    with session_factory() as session:
        failed_task_id = _seed_task(session, user_id=user, n_units=2)
    calls = 0

    def fail_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _valid_cards_json()}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    with session_factory() as session:
        process_active_tasks(
            session,
            settings=_SETTINGS,
            client_factory=lambda _k: DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(fail_handler)
            ),
        )
        session.commit()
        task = session.get(Task, failed_task_id)
        assert task is not None and task.status == "FAILED" and task.deck_id is not None
        staged_cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        failed_deck_id = task.deck_id
    assert len(staged_cards) == 1 and staged_cards[0].publication_state == "STAGED"
    # 删除失败任务（保留已发布卡语义）→ STAGED 残留必须清理
    with session_factory() as session:
        delete_task(session, user_id=user, task_id=failed_task_id, delete_generated_cards=False)
        session.commit()
        assert session.get(Task, failed_task_id) is None
        residual = session.scalars(select(Card).where(Card.deck_id == failed_deck_id)).all()
    assert residual == []  # STAGED 残留级联清理（不泄漏为孤儿 STAGED）
    # COMPLETED 任务：false → 已发布卡保留（source_task_id 置空）；true → 连卡删除
    with session_factory() as session:
        keep_task_id = _seed_task(session, user_id=user, n_units=1)
        delete_task_id = _seed_task(session, user_id=user, n_units=1)
    with session_factory() as session:
        process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
        keep_task = session.get(Task, keep_task_id)
        del_task_row = session.get(Task, delete_task_id)
        assert keep_task is not None and keep_task.deck_id is not None
        assert del_task_row is not None and del_task_row.deck_id is not None
        assert keep_task.status == "COMPLETED" and del_task_row.status == "COMPLETED"
        keep_deck_id, delete_deck_id = keep_task.deck_id, del_task_row.deck_id
    with session_factory() as session:
        delete_task(session, user_id=user, task_id=keep_task_id, delete_generated_cards=False)
        session.commit()
        assert session.get(Task, keep_task_id) is None
        kept = session.scalars(select(Card).where(Card.deck_id == keep_deck_id)).all()
        assert len(kept) == 1
        assert kept[0].source_task_id is None and kept[0].publication_state == "PUBLISHED"
    with session_factory() as session:
        delete_task(session, user_id=user, task_id=delete_task_id, delete_generated_cards=True)
        session.commit()
        assert session.get(Task, delete_task_id) is None
        gone = session.scalars(select(Card).where(Card.deck_id == delete_deck_id)).all()
    assert gone == []


def test_concurrency_zero_valid_cards_fails_task(
    session_factory: Callable[[], Session],
) -> None:
    """0 张有效卡整体失败（4.1 V25-D-23）：全部批次合法弃权（SOURCE_INSUFFICIENT）
    → 发布阶段 TASK_ZERO_CARDS → FAILED（不显示"完成 0 张"）；发布条件更新幂等。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=2)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"cards": []}, ensure_ascii=False)}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    with session_factory() as session:
        n = process_active_tasks(
            session,
            settings=_SETTINGS,
            client_factory=lambda _k: DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(handler)
            ),
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert task.status == "FAILED"
    assert task.error_code == "TASK_ZERO_CARDS"
    assert task.failure_stage == "PUBLISHING"
    assert task.generated_card_count == 0
    assert cards == []
    # 幂等：FAILED 后再次扫描不复活
    with session_factory() as session:
        n2 = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    assert n2 == 0
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.status == "FAILED"


def test_concurrency_stale_fail_task_does_not_overwrite_concurrent_result(
    session_factory: Callable[[], Session],
) -> None:
    """M-2 R1 修复：`_fail_task` 必须是条件更新（WHERE 读取快照的 status+stage）——
    stale worker（心跳超时 30 分钟后仍存活）异常路径不得覆盖并发 worker 已提交的
    COMPLETED（卡已 PUBLISHED，任务却 FAILED 的不一致）；也不得把并发转移中的
    任务（stage 已前进、另一 worker 在途）打死。RED：改造前 identity map 盲写
    无条件覆盖（flush 的 UPDATE 只有主键条件）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, n_units=1)
    # 场景 1：并发 worker 已完成原子发布（COMPLETED 已提交、卡 PUBLISHED）
    with session_factory() as stale_session:
        stale = stale_session.get(Task, task_id)  # stale worker 身份映射快照 GENERATING
        assert stale is not None and stale.status == "GENERATING"
        stale_session.commit()  # 读后提交释放事务，快照保留（expire_on_commit=False）
        with session_factory() as other:
            other.execute(
                update(Task)
                .where(Task.task_id == task_id, Task.status == "GENERATING")
                .values(status="COMPLETED", ended_at="2026-08-15T00:00:00.000Z", resumable=0)
            )
            other.commit()
        _fail_task(stale_session, stale, error_code=ErrorCode.GENERATION_FAILED.value)
        stale_session.commit()
    with session_factory() as session:
        row = session.get(Task, task_id)
        assert row is not None
        assert row.status == "COMPLETED"  # rowcount=0：不覆盖并发已提交的 COMPLETED
        assert row.error_code is None and row.resumable == 0
    # 场景 2：并发 worker 已把任务推进到 PUBLISHING（未终态，在途）→ 不打死
    with session_factory() as session:
        task2 = _seed_task(session, user_id=_uuid(), n_units=1)
    with session_factory() as stale_session:
        stale2 = stale_session.get(Task, task2)
        assert stale2 is not None and stale2.stage == "GENERATING"
        stale_session.commit()
        with session_factory() as other:
            other.execute(
                update(Task)
                .where(
                    Task.task_id == task2,
                    Task.status == "GENERATING",
                    Task.stage == "GENERATING",
                )
                .values(stage="PUBLISHING")
            )
            other.commit()
        _fail_task(stale_session, stale2, error_code=ErrorCode.GENERATION_FAILED.value)
        stale_session.commit()
    with session_factory() as session:
        row2 = session.get(Task, task2)
        assert row2 is not None
        assert row2.status == "GENERATING" and row2.stage == "PUBLISHING"  # 不覆盖在途转移
    # 场景 3（对照）：无并发转移 → 正常 FAILED + failure_stage 从快照派生 + resumable=0
    with session_factory() as session:
        task3 = _seed_task(session, user_id=_uuid(), n_units=1)
        stale3 = session.get(Task, task3)
        assert stale3 is not None
        _fail_task(session, stale3, error_code=ErrorCode.GENERATION_FAILED.value)
        session.commit()
    with session_factory() as session:
        row3 = session.get(Task, task3)
        assert row3 is not None
        assert row3.status == "FAILED"
        assert row3.error_code == ErrorCode.GENERATION_FAILED.value
        assert row3.failure_stage == "GENERATING"  # 从读取快照 stage 派生
        assert row3.resumable == 0
