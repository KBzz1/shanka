"""观测出口集成测试（契约 6.9/6.10/8.3）：批次列表（usage/版本/质量/cost 估算）、
quality-summary 聚合（user 隔离）、metrics 文本（llm/generation/batch 指标）。

mock transport 全链路驱动（LLM 升级管线：规划 → 生成 → 评分，一次 scan 衔接）：
COMPACT 2 章 → 6 生成单元（配额 BASIC 3/UNDERSTANDING 2/DEEP_QUESTION 1）→ 6 批
（1 单元 1 批）→ 每批 1 张合法卡 → 6 卡；评分 mock 返回确定性分数
（evidence=1/correctness=1/difficulty=1/learning=2，总分代码计算 5）；
usage 统一 hit=2/miss=8/output=5，model deepseek-v4-flash，rubric_version v3。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Chapter, LearningProject, Material, PdfFile, Task, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import scan_once as scan_tasks
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

# _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_PER_BATCH_COST = pytest.approx(2 * 0.5e-6 + 8 * 2e-6 + 5 * 8e-6)  # 0.000057 元/批


def _dispatch(request: httpx.Request) -> httpx.Response:
    """mock transport 全链路分派（模块级，供失败注入工厂复用）：<PLANNER_INPUT> →
    按请求配额产出锚定单元（引用请求内组页）；<SCORING_INPUT> → ID 守恒的确定性
    分数；其余（<GENERATION_SPEC>）→ 1 张合法卡（1 单元 1 批）。usage 统一
    hit=2/miss=8/output=5——cost 估算可算。"""

    body = json.loads(request.content)
    user = body["messages"][-1]["content"]
    if "<PLANNER_INPUT>" in user:
        payload = json.loads(user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0])
        chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
        units: list[dict[str, object]] = []
        for difficulty, quota_i in payload["difficulty_interval"].items():
            for _ in range(quota_i["max"]):
                units.append(
                    {
                        "source_chunk_ids": [chunk_ids[0]],
                        "learning_objective": f"知识点{len(units)}",
                        "target_difficulty": difficulty,
                        "card_type": "QUESTION",
                        "coverage_tier": "CORE",
                    }
                )
        content = json.dumps({"units": units}, ensure_ascii=False)
    elif "<SCORING_INPUT>" in user:
        payload = json.loads(user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0])
        content = json.dumps(
            {
                "scores": [
                    {
                        "generation_item_id": item["generation_item_id"],
                        "evidence_score": 1,
                        "correctness_score": 1,
                        "difficulty_score": 1,
                        "learning_value_score": 2,
                    }
                    for item in payload["items"]
                ]
            },
            ensure_ascii=False,
        )
    else:  # 生成调用：1 单元 1 批 → 每批 1 张合法卡
        content = json.dumps(
            {"cards": [{"type": "QUESTION", "question": "q0", "answer": "a0"}]},
            ensure_ascii=False,
        )
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


def _client_factory(api_key: str) -> DeepSeekClient:
    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(_dispatch))


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（后台循环间隔拉大隔离）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "obs.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_tasks
        rate_limit_ip_per_second=100,  # IP 限流隔离（本文件单测多次快速请求）
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def _seed_context(db_path: Path, *, user_id: str) -> dict[str, object]:
    """users 前置 + PDF + 2 章节 + 牌组 + 真实加密 ApiKey（COMPACT → 6 知识点 → 2 批）。

    PDF/牌组 user 域（tasks 归属校验）；ApiKey 用户域（P4-4 起——Core 直写只写所需列）。
    """
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用
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
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        project = LearningProject(
            project_id=_uuid(),
            user_id=user_id,
            name="P",
            chapters_confirmed_at="2026-08-11T00:00:00.000Z",
            version="2026-08-11T00:00:00.000Z",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
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
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
        deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
        session.flush()
        chapter_ids: list[str] = []
        for i in range(2):
            ch = Chapter(
                chapter_id=_uuid(),
                file_id=pdf.file_id,
                material_id=pdf.file_id,
                name=f"第{i + 1}章",
                start_page=i + 1,
                end_page=i + 2,
            )
            session.add(ch)
            session.flush()
            chapter_ids.append(ch.chapter_id)
        # 页文本（规划 worker 选页拆组的前提；第 2 页两章共享引用）
        persist_text_chunks(
            session,
            file_id=pdf.file_id,
            pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2, 3)],
            now="2026-08-11T00:00:00.000Z",
        )
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
        session.commit()
    return {
        "project_id": project.project_id,
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
    }


def _payload(seed: dict[str, object]) -> dict[str, object]:
    return {
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "coverage_mode": "COMPACT",
            "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
        },
    }


def _run_task(client: TestClient, db_path: Path, *, user: dict[str, str]) -> tuple[str, str]:
    """V2.5 完整生命周期：POST 创建 DRAFT → 请求样卡 → 显式扫描（样卡 worker）→
    start → 显式扫描（规划/生成/评分衔接）→ 返回 (task_id, file_id)。"""
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    resp = client.post(
        f"/projects/{seed['project_id']}/tasks",
        json=_payload(seed),
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    n = scan_tasks(factory, settings=_SETTINGS, client_factory=_client_factory)
    assert n == 1  # 样卡 worker 完成（SAMPLE_GENERATING → AWAITING）
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    n = scan_tasks(factory, settings=_SETTINGS, client_factory=_client_factory)
    assert n >= 1  # 规划 + 首轮生成（work_quantum 4 批/轮）
    # worker 单轮受 generation_work_quantum_batches 限制：排空至无进展 → COMPLETED
    for _ in range(10):
        if scan_tasks(factory, settings=_SETTINGS, client_factory=_client_factory) == 0:
            break
    return task_id, str(seed["file_id"])


def _labeled_value(text: str, name: str, fragments: list[str]) -> float:
    """Prometheus 文本中指定 name+label 片段的数值（label 顺序不敏感）；不存在返回 0。"""
    for line in text.splitlines():
        if not line.startswith(f"{name}{{"):
            continue
        labels = line.split("{", 1)[1].split("}", 1)[0]
        if all(frag in labels for frag in fragments):
            return float(line.split()[-1])
    return 0.0


def _plain_value(text: str, metric_line: str) -> float:
    """Prometheus 文本中无 label 指标行的数值；不存在返回 0。"""
    for line in text.splitlines():
        if line.startswith(metric_line):
            return float(line.split()[-1])
    return 0.0


def test_batches_endpoint_lists_usage_versions_quality_and_cost(
    ctx: tuple[TestClient, Path],
) -> None:
    """GET /tasks/{task_id}/batches：状态/retry/质量/usage/版本/model/http_status/duration/cost。"""
    client, db_path = ctx
    user = _user(client)
    task_id, _file_id = _run_task(client, db_path, user=user)
    resp = client.get(f"/tasks/{task_id}/batches", headers=user)
    assert resp.status_code == 200
    items = resp.json()["items"]
    print("REAL_BATCH_ITEMS:", len(items))
    assert len(items) == 6  # 1 单元 1 批（6 生成单元）
    first = items[0]
    assert first["status"] == "SUCCEEDED"
    assert first["retry_count"] == 0
    assert len(first["generated_item_ids"]) == 1  # 本批 1 张合法卡（批=单元）
    # usage（FR-11 Prompt Cache 记录）
    assert first["cache_hit_tokens"] == 2
    assert first["cache_miss_tokens"] == 8
    assert first["output_tokens"] == 5
    # 版本/model/http_status/duration
    assert first["model"] == "deepseek-v4-flash"
    assert first["prompt_version"] == "v6"
    assert first["schema_version"] == "v3"
    assert first["rubric_version"] == "v3"
    assert first["http_status"] == 200
    assert isinstance(first["duration_ms"], int)
    assert first["request_id"] is None  # carry-forward 决策：视图保留字段，R1 live 上游 id 透传
    # 质量（批=单元：分布按单元锚定单值）
    assert first["coverage_rate"] == 1.0  # 1 卡 / 1 单元
    assert first["duplicate_rate"] == 0.0
    assert set(first["difficulty_distribution"]) <= {"BASIC", "UNDERSTANDING", "DEEP_QUESTION"}
    assert sum(first["difficulty_distribution"].values()) == 1
    assert first["difficulty_deviation"] == 0.0  # V5A 简化（观测字段结构完整）
    # 成本估算（8.4 价格常量，仅观测）
    assert first["cost_estimate"] == _PER_BATCH_COST


def test_quality_summary_aggregates_by_model_pdf_difficulty(
    ctx: tuple[TestClient, Path],
) -> None:
    """GET /observability/quality-summary：Rubric 均分/覆盖/重复率/完成率/成本，group_by 三种分组。"""
    client, db_path = ctx
    user = _user(client)
    task_id, file_id = _run_task(client, db_path, user=user)
    _ = task_id  # 完成任务存在即可（聚合跨任务）

    # 默认 group_by=model & days=30
    resp = client.get("/observability/quality-summary", headers=user)
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "model"
    assert body["days"] == 30
    assert len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["key"] == "deepseek-v4-flash"
    assert g["rubric_version"] == "v3"  # 卡经批次归属 → 批次 rubric_version
    assert g["card_count"] == 6
    assert g["eligible_card_count"] == 6
    assert g["scored_card_count"] == 6
    assert g["sampling_rate"] == 1.0
    assert g["evidence_avg"] == 1.0
    assert g["correctness_avg"] == 1.0
    assert g["difficulty_avg"] == 1.0
    assert g["learning_value_avg"] == 2.0
    assert g["coverage_avg"] == 1.0
    assert g["duplicate_avg"] == 0.0
    assert g["task_completion_rate"] == 1.0  # 1 个 COMPLETED / 1 个任务
    assert g["cost_estimate"]["total"] == pytest.approx(0.000342)  # 6 批 × 0.000057
    assert g["cost_estimate"]["cache_hit"] > 0
    assert g["cost_estimate"]["scope"] == "generation-stage-only"  # 不引入账本双计

    # group_by=pdf：键 = PDF file_id
    resp = client.get("/observability/quality-summary", params={"group_by": "pdf"}, headers=user)
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["key"] == file_id
    assert groups[0]["card_count"] == 6

    # group_by=difficulty：键 = generation_unit_id → 单元 target_difficulty（确定性）；
    # 配额 BASIC 3 / UNDERSTANDING 2 / DEEP_QUESTION 1 → 每单元 1 卡同分布。
    resp = client.get(
        "/observability/quality-summary", params={"group_by": "difficulty"}, headers=user
    )
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    by_key = {g["key"]: g for g in groups}
    assert set(by_key) == {"BASIC", "UNDERSTANDING", "DEEP_QUESTION"}
    # 密度制（V25-D-25）：2 章 COMPACT，每章区间 (1,1) 经极小占比兜底 → 每难度各 1 单元
    assert by_key["BASIC"]["card_count"] == 2
    assert by_key["UNDERSTANDING"]["card_count"] == 2
    assert by_key["DEEP_QUESTION"]["card_count"] == 2


def test_quality_summary_isolates_by_device(ctx: tuple[TestClient, Path]) -> None:
    """6.10 隔离口径：quality-summary 按当前 user 聚合；批次列表跨用户 404。"""
    client, db_path = ctx
    user = _user(client)
    task_id, _file_id = _run_task(client, db_path, user=user)

    other = _user(client, "user2", "pass-2222")
    resp = client.get("/observability/quality-summary", headers=other)
    assert resp.status_code == 200
    assert resp.json()["groups"] == []  # 其他用户不可见
    resp = client.get(f"/tasks/{task_id}/batches", headers=other)
    assert resp.status_code == 404  # 统一 404，不暴露资源存在性


def test_metrics_text_includes_llm_generation_batch_metrics(
    ctx: tuple[TestClient, Path],
) -> None:
    """8.3：mock transport 驱动全管线（规划 2 次 + 生成 6 次 + 评分 5 次）后
    /metrics 文本含 llm/generation/batch 指标（差值断言）。"""
    client, db_path = ctx
    user = _user(client)
    before = client.get("/metrics").text
    _run_task(client, db_path, user=user)
    after = client.get("/metrics").text

    # llm（样卡 3 次 + 生成 6 批 + 评分 6 次 = 15 次 chat；规划调用不在 llm 指标口径；
    # 密度制 V25-D-25 后每章难度兜底产出 6 生成单元）
    ok_labels = ['model="deepseek-v4-flash"', 'http_status="200"']
    assert (
        _labeled_value(after, "llm_requests_total", ok_labels)
        - _labeled_value(before, "llm_requests_total", ok_labels)
    ) == 15.0
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="cache_hit"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="cache_hit"'])
    ) == 30.0  # 15 次 × 2
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="cache_miss"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="cache_miss"'])
    ) == 120.0  # 15 次 × 8
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="output"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="output"'])
    ) == 75.0  # 15 次 × 5
    assert (
        _plain_value(after, "llm_request_duration_seconds_count")
        - _plain_value(before, "llm_request_duration_seconds_count")
    ) == 15.0
    # generation（1 个任务 COMPLETED）
    assert (
        _labeled_value(after, "generation_tasks_total", ['result="COMPLETED"'])
        - _labeled_value(before, "generation_tasks_total", ['result="COMPLETED"'])
    ) == 1.0
    assert (
        _plain_value(after, "generation_tasks_duration_seconds_count")
        - _plain_value(before, "generation_tasks_duration_seconds_count")
    ) == 1.0
    # batch（本次无重试：差值 0，指标已注册且可解析）
    assert (
        _plain_value(after, "batch_retry_total") - _plain_value(before, "batch_retry_total")
    ) == 0.0


def test_quality_summary_excludes_staged_and_failed_task_cards(
    ctx: tuple[TestClient, Path],
) -> None:
    """R1 I-1：quality-summary 是全仓库唯一向已认证用户暴露的卡查询路径——必须与
    用户侧查询同口径（统一可见谓词 3.9）：生成中任务（GENERATING）与已 FAILED
    任务的 STAGED 卡不得计入 card_count 与各维度分数；只计已发布卡。

    RED 语义：改造前该查询无谓词，STAGED 卡照常计入（6 张已发布 + 2 张 STAGED
    = 8）；覆盖率/完成率等批次级聚合不受卡谓词影响（分母语义不变）。
    """
    client, db_path = ctx
    user = _user(client)
    _run_task(client, db_path, user=user)  # 1 个 COMPLETED 任务（6 卡 PUBLISHED）

    def _create_scan_task(handler: Any) -> str:
        """完整生命周期但注入自定义 transport handler（规划/评分走 _dispatch，
        生成调用按 handler 注入失败）→ 返回 task_id。"""
        seed = _seed_context(db_path, user_id=_user_id(db_path))
        resp = client.post(
            f"/projects/{seed['project_id']}/tasks",
            json=_payload(seed),
            headers={**user, **_idem()},
        )
        assert resp.status_code == 201
        task_id = str(resp.json()["task_id"])
        factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
        assert (
            client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
        )
        n = scan_tasks(factory, settings=_SETTINGS, client_factory=_client_factory)
        assert n == 1  # 样卡 worker 完成
        assert (
            client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
        )
        n = scan_tasks(
            factory,
            settings=_SETTINGS,
            client_factory=lambda _k: DeepSeekClient(
                _SETTINGS, transport=httpx.MockTransport(handler)
            ),
        )
        assert n == 1  # 规划 + 批 1 完成（批 2 注入失败/崩溃）
        return task_id

    def _generation_fail_handler() -> Any:
        """生成调用第 2 次起 401（批 1 成功 1 卡 → 任务 FAILED + 1 张 STAGED 卡）。"""
        state = {"gen_calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user_msg = body["messages"][-1]["content"]
            if "<PLANNER_INPUT>" in user_msg or "<SCORING_INPUT>" in user_msg:
                return _dispatch(request)
            state["gen_calls"] += 1
            if state["gen_calls"] == 2:
                return httpx.Response(401, json={"error": {"message": "invalid api key"}})
            return _dispatch(request)

        return handler

    def _generation_crash_handler() -> Any:
        """生成调用第 2 次 SystemExit（模拟 worker 崩溃）→ 任务停留 GENERATING +
        批 1 卡 STAGED 隔离（生成中任务的卡）。"""
        state = {"gen_calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user_msg = body["messages"][-1]["content"]
            if "<PLANNER_INPUT>" in user_msg or "<SCORING_INPUT>" in user_msg:
                return _dispatch(request)
            state["gen_calls"] += 1
            if state["gen_calls"] == 2:
                raise SystemExit("模拟崩溃：批 2 处理中断")
            return _dispatch(request)

        return handler

    # FAILED 任务：批 1 成功（1 张 STAGED 卡）、批 2 401 → 任务 FAILED
    _create_scan_task(_generation_fail_handler())
    with _db_factory(db_path)() as session:
        failed_tasks = session.scalars(select(Task).where(Task.status == "FAILED")).all()
        assert len(failed_tasks) == 1
    # 生成中任务：批 1 成功（1 张 STAGED 卡）、批 2 崩溃 → 任务停留 GENERATING
    with pytest.raises(SystemExit):
        _create_scan_task(_generation_crash_handler())
    with _db_factory(db_path)() as session:
        gen_tasks = session.scalars(select(Task).where(Task.status == "GENERATING")).all()
        assert len(gen_tasks) == 1

    resp = client.get("/observability/quality-summary", headers=user)
    assert resp.status_code == 200
    by_key = {g["key"]: g for g in resp.json()["groups"]}
    g = by_key["deepseek-v4-flash"]
    assert g["card_count"] == 6  # 只计已发布卡（RED：改造前 STAGED 卡计入 → 8）
    assert g["scored_card_count"] == 6
    assert g["sampling_rate"] == 1.0
    # 批次级聚合语义不受卡谓词影响（分母随 SKIPPED 覆盖率的既有语义不变）
    assert g["task_completion_rate"] == pytest.approx(1 / 3)  # 1 COMPLETED / 3 任务
    assert g["coverage_avg"] == 1.0  # 3 个 SUCCEEDED 批次覆盖 1.0（失败任务批 1 仍计入）
    # PENDING 批次（model NULL）归 unknown 组：无卡（0 张 STAGED/PUBLISHED 经批次归属）
    assert by_key["unknown"]["card_count"] == 0
    assert by_key["unknown"]["task_completion_rate"] == 0.0


def _db_factory(db_path: Path) -> Callable[[], Session]:
    """测试内 DB 工厂（engine 每次新建，避免跨连接锁）。"""
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
