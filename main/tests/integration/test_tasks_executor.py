"""任务执行器集成测试：V5A adapter 分批生成入库/状态机/防重（mock transport，不触网）。

种子写入真实加密 Key（executor 解密路径）；scan_once/process_active_tasks 注入
settings + client_factory（mock transport），生产缺省路径不在此验证。
"""

import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY, generate_latest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.samples import DifficultyRatio, GenerationConfig
from infra.db.models import Base, Card, KnowledgePoint, Task, TextChunk, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.generation.batches import plan_batches
from services.tasks.executor import process_active_tasks
from services.tasks.service import create_task

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


@pytest.fixture
def session_factory(tmp_path: Path) -> Callable[[], Session]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'exec.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _uuid() -> str:
    return str(uuid.uuid4())


def _seed_task(session: Session, *, user_id: str, coverage_mode: str = "COMPACT") -> str:
    """种子：DRAFT 任务直转 GENERATING（stage=GENERATING）+ 页文本 + 生成单元
    （锚定难度/卡型/来源页）+ 按单元建批（generation_unit_id 必填——LLM 升级管线
    spec §7 批=单元，1 单元 1 批）。

    V2.5 起 create_task 只落 DRAFT（自动保存）；生成路径测试绕过样卡/规划
    worker，聚焦批次语义——规划执行路径由 test_planning_executor.py 覆盖。
    单元数 = 每章基础 3 × 密度系数（COMPACT=3 / BALANCED=6）——旧规划配额语义，
    测试直接构造；目标难度按 0.4/0.4/0.2 循环锚定。
    """
    from infra.db.models import ApiKey, Chapter, LearningProject, PdfFile
    from services.decks.service import create_deck
    from services.pdf.text_chunks import persist_text_chunks

    # 守卫插入：同 user 二次建任务（防回退用例）复用已存在的 users/api_keys 行
    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
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
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at="2026-08-11T00:00:00.000Z",
        version="2026-08-11T00:00:00.000Z",
        created_at="2026-08-11T00:00:00.000Z",
        updated_at="2026-08-11T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
    deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
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
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now="2026-08-11T00:00:00.000Z",
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
        now="2026-08-11T00:00:00.000Z",
    )
    task.status = "GENERATING"  # V2.5 七态：跳过样卡阶段直入生成（批次语义聚焦）
    task.stage = "GENERATING"
    task.updated_at = "2026-08-11T00:00:00.000Z"
    session.flush()
    chunks = session.scalars(
        select(TextChunk).where(TextChunk.file_id == pdf.file_id).order_by(TextChunk.page_number)
    ).all()
    diffs = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]  # V2.5 改名（3.5）
    n_kps = {"COMPACT": 3, "BALANCED": 6}.get(coverage_mode, 3)
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
    # 批次 = 单元（spec §7：1 单元 1 批，generation_unit_id 显式外键）
    plan_batches(
        session, task_id=task.task_id, generation_units=kps, now="2026-08-11T00:00:00.000Z"
    )
    session.commit()
    return task.task_id


def _valid_cards_json(n: int = 1) -> str:
    """每批 1 张合法卡（LLM 升级管线 spec §7：批=单元，generator-output schema v2
    maxItems=1）——3 单元任务 → 3 批 → 3 卡（每单元一张）。"""
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


def _seed_planning_task(session: Session, *, user_id: str) -> str:
    """GENERATING+PLANNING 任务（start 后状态）+ 章节 + 页文本（text_chunks）：
    规划 worker 全流程基座。"""
    from infra.db.models import ApiKey, Chapter, LearningProject, PdfFile
    from services.decks.service import create_deck
    from services.pdf.text_chunks import persist_text_chunks

    if session.get(User, user_id) is None:
        session.add(
            User(
                user_id=user_id,
                username=f"u-{user_id[:8]}",
                email=f"u-{user_id[:8]}@example.com",
                password_hash="x",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
    pdf = PdfFile(
        file_id=_uuid(),
        user_id=user_id,
        filename="p.pdf",
        storage_key=_uuid(),
        size_bytes=1,
        status="PARSED",
        created_at="2026-08-11T00:00:00.000Z",
    )
    session.add(pdf)
    session.flush()
    project = LearningProject(
        project_id=_uuid(),
        user_id=user_id,
        file_id=pdf.file_id,
        name="P",
        chapters_confirmed_at="2026-08-11T00:00:00.000Z",
        version="2026-08-11T00:00:00.000Z",
        created_at="2026-08-11T00:00:00.000Z",
        updated_at="2026-08-11T00:00:00.000Z",
    )
    session.add(project)
    session.flush()
    deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
    deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
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
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
    persist_text_chunks(
        session,
        file_id=pdf.file_id,
        pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2)],
        now="2026-08-11T00:00:00.000Z",
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
        now="2026-08-11T00:00:00.000Z",
    )
    # start 后状态（4.1）：AWAITING_SAMPLE_CONFIRMATION → GENERATING + stage=PLANNING
    task.status = "GENERATING"
    task.stage = "PLANNING"
    task.updated_at = "2026-08-11T00:00:00.000Z"
    session.commit()
    return task.task_id


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport 分派：<SCORING_INPUT> → 分数；其余（<GENERATION_SPEC>）→ 每批 1 卡。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        content = _scoring_content(request) if "<SCORING_INPUT>" in user else _valid_cards_json()
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

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


def test_executor_completes_task_and_inserts_cards(session_factory: Callable[[], Session]) -> None:
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
    assert n == 1
    assert task.status == "COMPLETED"
    assert len(cards) == len(kps)  # 每知识点一张卡
    assert task.generated_card_count == len(cards)
    assert all(c.source == "GENERATED" for c in cards)


def test_executor_no_duplicate_generation_items(session_factory: Callable[[], Session]) -> None:
    """generation_item_id 部分唯一索引防重：二次执行不重复入库。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)
    with session_factory() as session:
        process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    # 已完成任务不再处理
    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    assert n == 0
    with session_factory() as session:
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
        item_ids = [c.generation_item_id for c in cards]
    assert len(item_ids) == len(set(item_ids))  # 无重复


def test_executor_same_chapter_second_task_still_generates(
    session_factory: Callable[[], Session],
) -> None:
    """F-1 防回退：generation_item_id seed 含任务维度——同设备同章节二次任务不互相去重。"""
    user = _uuid()
    with session_factory() as session:
        task1 = _seed_task(session, user_id=user)
        task2 = _seed_task(session, user_id=user)  # 同章节二次任务（新 task_id）
    with session_factory() as session:
        process_active_tasks(session, settings=_SETTINGS, client_factory=_client_factory)
        session.commit()
    with session_factory() as session:
        for task_id in (task1, task2):
            task = session.get(Task, task_id)
            assert task is not None
            assert task.status == "COMPLETED"
            assert task.generated_card_count > 0  # 若无 task 维度，第二个任务会被全局去重清 0


def _metric_value(name: str, fragments: list[str]) -> float:
    """Prometheus 文本中指定 name+label 片段的数值（label 顺序不敏感）；不存在返回 0。"""
    for line in generate_latest(REGISTRY).decode().splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        labels = line.split("{", 1)[1].split("}", 1)[0]
        if all(frag in labels for frag in fragments):
            return float(line.split()[-1])
    return 0.0


def test_executor_system_failure_fails_task_and_keeps_cards(
    session_factory: Callable[[], Session],
) -> None:
    """F-2 系统级失败路径：第 2 批 transport 401 → adapter 抛 API_KEY_UNAVAILABLE →
    任务 FAILED + error_code + resumable=0 + 已入库卡保留 + generation_tasks_total{FAILED} 计数。

    BALANCED 密度 → 6 单元 → 6 批（批=单元）：第 1 批成功（1 卡入库），
    第 2 批 401（Key 失效，retryable=False）→ executor 上抛 AppError → _fail_task
    （4.1 系统级失败）。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user, coverage_mode="BALANCED")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": _valid_cards_json()}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                    "model": "deepseek-v4-flash",
                },
            )
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    before = _metric_value("generation_tasks_total", ['result="FAILED"'])
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    after = _metric_value("generation_tasks_total", ['result="FAILED"'])
    assert n == 1
    assert calls == 2  # 第 1 批成功、第 2 批 401
    assert task.status == "FAILED"
    assert task.error_code == "API_KEY_UNAVAILABLE"
    assert task.failure_stage == "GENERATING"
    assert task.resumable == 0
    assert task.ended_at == task.updated_at
    assert len(cards) == 1  # 第 1 批已入库卡保留（4.1）
    assert after - before == 1.0  # 8.3：系统级失败也计数


def test_executor_full_flow_plan_then_generate(
    session_factory: Callable[[], Session],
) -> None:
    """T9/T11 接线：一次扫描 = 规划 worker（CAS 抢占 + 规划落库）→ 生成 worker（批生成）
    → 评分 worker（SCORING 回写）→ COMPLETED。

    mock 首次 chat 返回合法 planner 单元（引用请求内组页），随后返回 1 张合法卡
    （1 单元 = 1 批 = 1 卡），评分调用（<SCORING_INPUT>）返回 ID 守恒的分数；
    断言规划/生成/评分在同一次 process_active_tasks 中衔接。
    """
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_planning_task(session, user_id=user)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        if "<SCORING_INPUT>" in user:  # 评分调用：ID 集合守恒的合法分数
            payload = json.loads(
                user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0]
            )
            content = json.dumps(
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
        elif calls == 1:  # 规划调用：从 <PLANNER_INPUT> 提取组页 → 合法单元
            payload = json.loads(
                user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0]
            )
            chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
            content = json.dumps(
                {
                    "units": [
                        {
                            "source_chunk_ids": [chunk_ids[0]],
                            "learning_objective": "全流程目标",
                            "target_difficulty": "BASIC",
                            "card_type": "QUESTION",
                            "coverage_tier": "CORE",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        else:  # 生成调用：1 张合法卡（1 单元 1 批）
            content = _valid_cards_json(1)
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

    def client_factory(_api_key: str) -> DeepSeekClient:
        # 每次调用返回新 client：executor 在规划阶段结束后 close 规划 client，
        # 生成阶段经 factory 重建（与生产路径一致）
        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    with session_factory() as session:
        n = process_active_tasks(session, settings=_SETTINGS, client_factory=client_factory)
        session.commit()
        task = session.get(Task, task_id)
        assert task is not None and task.deck_id is not None
        kps = session.scalars(select(KnowledgePoint).where(KnowledgePoint.task_id == task_id)).all()
        cards = session.scalars(select(Card).where(Card.deck_id == task.deck_id)).all()
    assert n == 1
    assert calls == 3  # 1 次规划 + 1 次生成 + 1 次评分（同一扫描轮内衔接，T11 SCORING 阶段）
    assert task.status == "COMPLETED"
    assert len(kps) == 1
    assert kps[0].topic == "全流程目标"
    assert len(cards) == 1
    assert cards[0].rubric_total_score == 9  # 评分回写（代码计算四维和）
    assert task.generated_card_count == 1


def test_executor_zero_cards_failure_counts_metric(
    session_factory: Callable[[], Session],
) -> None:
    """M-3 R1 修复：TASK_ZERO_CARDS 发布失败同样计入 generation_tasks_total{FAILED}
    （8.3 全终态路径口径——其余终态路径 executor.py 均上报，仅发布 0 卡分支此前漏报）。

    RED 语义：改造前 0 张有效卡整体失败不出现在 GENERATION_TASKS 指标（差值 0）。"""
    user = _uuid()
    with session_factory() as session:
        task_id = _seed_task(session, user_id=user)

    def handler(request: httpx.Request) -> httpx.Response:
        # 全部批次合法弃权（显式空数组）→ 发布阶段 TASK_ZERO_CARDS（4.1 V25-D-23）
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"cards": []}, ensure_ascii=False)}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))
    before = _metric_value("generation_tasks_total", ['result="FAILED"'])
    with session_factory() as session:
        n = process_active_tasks(
            session, settings=_SETTINGS, client_factory=lambda _api_key: client
        )
        session.commit()
        task = session.get(Task, task_id)
    after = _metric_value("generation_tasks_total", ['result="FAILED"'])
    assert n == 1
    assert task is not None
    assert task.status == "FAILED"
    assert task.error_code == "TASK_ZERO_CARDS"
    assert after - before == 1.0  # 8.3：发布失败（0 张有效卡）同样计数
