"""观测出口集成测试（契约 6.9/6.10/8.3）：批次列表（usage/版本/质量/cost 估算）、
quality-summary 聚合（device 隔离）、metrics 文本（llm/generation/batch 指标）。

mock transport 驱动两批（COMPACT 2 章 = 6 知识点 = 2 批）后经 API 断言：
每批 3 张合法卡（q0..q2/a0..a2），usage hit=2/miss=8/output=5，model deepseek-v4-flash。
Rubric（deterministic fake）：evidence=1（q 长 2）、correctness=1（a 长 2）、
difficulty 按 kp priority 轮换（0/1/2 → 平均 1.0）、learning=2（无 explanation）。
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Chapter, Device, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.tasks.executor import scan_once as scan_tasks

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

_PER_BATCH_COST = pytest.approx(2 * 0.5e-6 + 8 * 2e-6 + 5 * 8e-6)  # 0.000057 元/批


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport：每批 3 张合法卡 + usage（cache hit/miss）——cost 估算可算。"""

    def handler(request: httpx.Request) -> httpx.Response:
        cards = [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(3)]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}
                ],
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


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_context(db_path: Path, *, device_id: str) -> dict[str, object]:
    """devices 前置 + PDF + 2 章节 + 牌组 + 真实加密 ApiKey（COMPACT → 6 知识点 → 2 批）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            device_id=device_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
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
        session.add(
            ApiKey(
                device_id=device_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        session.commit()
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _payload(seed: dict[str, object]) -> dict[str, object]:
    return {
        "file_id": seed["file_id"],
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "quantity_tendency": "COMPACT",
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
    }


def _run_task(client: TestClient, db_path: Path, *, device: dict[str, str]) -> tuple[str, str]:
    """POST 任务 → 显式 executor 扫描（mock transport 两批）→ 返回 (task_id, file_id)。"""
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    n = scan_tasks(factory, settings=_SETTINGS, client_factory=_client_factory)
    assert n == 1  # 单任务执行完毕（两批）
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
    device = _device()
    task_id, _file_id = _run_task(client, db_path, device=device)
    resp = client.get(f"/tasks/{task_id}/batches", headers=device)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    first = items[0]
    assert first["status"] == "SUCCEEDED"
    assert first["retry_count"] == 0
    assert len(first["generated_item_ids"]) == 3  # 本批 3 张合法卡
    # usage（FR-11 Prompt Cache 记录）
    assert first["cache_hit_tokens"] == 2
    assert first["cache_miss_tokens"] == 8
    assert first["output_tokens"] == 5
    # 版本/model/http_status/duration
    assert first["model"] == "deepseek-v4-flash"
    assert first["prompt_version"] == "v2"  # generator v2（R1 canary 修复）
    assert first["schema_version"] == "v1"
    assert first["rubric_version"] == "v1"
    assert first["http_status"] == 200
    assert isinstance(first["duration_ms"], int)
    assert first["request_id"] is None  # carry-forward 决策：视图保留字段，R1 live 上游 id 透传
    # 质量（Rubric 批次汇总；kp priority 并列无稳定排序（SQLite）→ 只断言档位集合与卡数）
    assert first["coverage_rate"] == 1.0  # 3 卡 / 3 知识点
    assert first["duplicate_rate"] == 0.0
    assert set(first["difficulty_distribution"]) <= {"BASIC", "UNDERSTANDING", "APPLICATION"}
    assert sum(first["difficulty_distribution"].values()) == 3
    assert first["difficulty_deviation"] == 0.0  # V5A 简化（观测字段结构完整）
    # 成本估算（8.4 价格常量，仅观测）
    assert first["cost_estimate"] == _PER_BATCH_COST


def test_quality_summary_aggregates_by_model_pdf_difficulty(
    ctx: tuple[TestClient, Path],
) -> None:
    """GET /observability/quality-summary：Rubric 均分/覆盖/重复率/完成率/成本，group_by 三种分组。"""
    client, db_path = ctx
    device = _device()
    task_id, file_id = _run_task(client, db_path, device=device)
    _ = task_id  # 完成任务存在即可（聚合跨任务）

    # 默认 group_by=model & days=30
    resp = client.get("/observability/quality-summary", headers=device)
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_by"] == "model"
    assert body["days"] == 30
    assert len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["key"] == "deepseek-v4-flash"
    assert g["card_count"] == 6
    assert g["evidence_avg"] == 1.0  # q 长度 < 10
    assert g["correctness_avg"] == 1.0  # a 长度 < 10
    assert g["difficulty_avg"] == 1.0  # 难度轮换 0/1/2 平均
    assert g["learning_value_avg"] == 2.0  # 无 explanation
    assert g["coverage_avg"] == 1.0
    assert g["duplicate_avg"] == 0.0
    assert g["task_completion_rate"] == 1.0  # 1 个 COMPLETED / 1 个任务
    assert g["cost_estimate"]["total"] == pytest.approx(0.000114)  # 2 批 × 0.000057
    assert g["cost_estimate"]["cache_hit"] > 0

    # group_by=pdf：键 = PDF file_id
    resp = client.get("/observability/quality-summary", params={"group_by": "pdf"}, headers=device)
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["key"] == file_id
    assert groups[0]["card_count"] == 6

    # group_by=difficulty：键 = 批次难度分布计数最大档（同数取字典序最小，确定性）。
    # kp 按 priority 并列排序不稳定（SQLite 不保证并列顺序）→ 只断言档位集合与总卡数。
    resp = client.get(
        "/observability/quality-summary", params={"group_by": "difficulty"}, headers=device
    )
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert {g["key"] for g in groups} <= {"BASIC", "UNDERSTANDING", "APPLICATION"}
    assert sum(g["card_count"] for g in groups) == 6


def test_quality_summary_isolates_by_device(ctx: tuple[TestClient, Path]) -> None:
    """6.10 隔离口径：quality-summary 按当前 device 聚合；批次列表跨设备 404。"""
    client, db_path = ctx
    device = _device()
    task_id, _file_id = _run_task(client, db_path, device=device)

    other = _device()
    resp = client.get("/observability/quality-summary", headers=other)
    assert resp.status_code == 200
    assert resp.json()["groups"] == []  # 其他设备不可见
    resp = client.get(f"/tasks/{task_id}/batches", headers=other)
    assert resp.status_code == 404  # 统一 404，不暴露资源存在性


def test_metrics_text_includes_llm_generation_batch_metrics(
    ctx: tuple[TestClient, Path],
) -> None:
    """8.3：mock transport 驱动两批后 /metrics 文本含 llm/generation/batch 指标（差值断言）。"""
    client, db_path = ctx
    device = _device()
    before = client.get("/metrics").text
    _run_task(client, db_path, device=device)
    after = client.get("/metrics").text

    # llm（每批一次 chat；2 批）
    ok_labels = ['model="deepseek-v4-flash"', 'http_status="200"']
    assert (
        _labeled_value(after, "llm_requests_total", ok_labels)
        - _labeled_value(before, "llm_requests_total", ok_labels)
    ) == 2.0
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="cache_hit"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="cache_hit"'])
    ) == 4.0  # 2 批 × 2
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="cache_miss"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="cache_miss"'])
    ) == 16.0  # 2 批 × 8
    assert (
        _labeled_value(after, "llm_tokens_total", ['kind="output"'])
        - _labeled_value(before, "llm_tokens_total", ['kind="output"'])
    ) == 10.0  # 2 批 × 5
    assert (
        _plain_value(after, "llm_request_duration_seconds_count")
        - _plain_value(before, "llm_request_duration_seconds_count")
    ) == 2.0
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


def test_cancel_metric_counts_transition_once(ctx: tuple[TestClient, Path]) -> None:
    """F-1 回归：generation_tasks_total CANCELLED 只在实际状态转移时计数。

    同任务不同幂等键重复取消（任务已终态 → service 早返回不转移）与同键重放
    （execute_idempotent 快照，不重跑 biz）均不重复 inc（差值断言）。
    """
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201  # 任务创建即 RUNNING（未跑 executor）
    task_id = resp.json()["task_id"]
    labels = ['result="CANCELLED"']
    before = _labeled_value(client.get("/metrics").text, "generation_tasks_total", labels)
    key_a = _idem()
    # 首次取消（RUNNING → CANCELLED）：计数 +1
    r1 = client.post(f"/tasks/{task_id}/cancel", headers={**device, **key_a})
    assert r1.status_code == 200 and r1.json()["status"] == "CANCELLED"
    # 不同幂等键重复取消（任务已终态）：service 早返回，不转移不计数
    r2 = client.post(f"/tasks/{task_id}/cancel", headers={**device, **_idem()})
    assert r2.status_code == 200 and r2.json()["status"] == "CANCELLED"
    # 同键重放：走 execute_idempotent 快照，不重跑 biz
    r3 = client.post(f"/tasks/{task_id}/cancel", headers={**device, **key_a})
    assert r3.status_code == 200 and r3.json() == r1.json()
    after = _labeled_value(client.get("/metrics").text, "generation_tasks_total", labels)
    assert after - before == 1.0
