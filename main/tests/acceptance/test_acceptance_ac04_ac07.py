"""验收测试：AC-04 正式生成与入库 + AC-07 质量与缓存数据（PRD 9；迁移 schema + HTTP + mock transport）。

映射（PRD AC-04 三条 / AC-07 三条）：
AC-04-a 按知识点分批生成正式卡片 → POST /tasks → executor 扫描（mock transport 合法卡）
        → 任务 COMPLETED + 合法卡入库（Schema 通过）
AC-04-b 只有通过 Schema 校验的卡片入库 → 非法卡批次 SKIPPED，cards 无行
AC-04-c Rubric 评分不影响 Schema 合法卡入库 → 低分但 Schema 合法的卡仍全部入库
AC-04-d 不因 Rubric 执行自动修复/淘汰/补生成 → 低分批次 SUCCEEDED 不重试（retry_count=0）、
        批次数不增加（total==completed）、低分卡不淘汰（仍在牌组）
AC-07-a 单卡 Rubric 评分 + 整批质量记录 → GET /tasks/{id}/batches 含 rubric_version/质量分布；
        卡片 5 个 Rubric 分数字段非 null
AC-07-b Prompt Cache 命中/未命中/输出 Token 记录 → batches items 含 cache_hit/miss + output tokens
AC-07-c Rubric/Cache 异常不影响入库规则 → usage 缺失（token 观测 None）仍正常入库 SUCCEEDED

后台循环间隔拉大（3600s）隔离：测试显式调 executor.scan_once（test_tasks_api 同款"显式
scan_once"模式）；种子直写迁移后 DB（devices 前置 + 真实加密 Key——executor 解密路径）。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Batch, Card, Chapter, Device, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.tasks.executor import scan_once as scan_tasks

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


def _handler(cards: list[dict[str, object]], *, with_usage: bool = True) -> httpx.Response:
    """构造 mock transport 响应体：卡片 + Prompt Cache usage + model。"""
    body: dict[str, object] = {
        "choices": [{"message": {"content": json.dumps({"cards": cards}, ensure_ascii=False)}}],
        "model": "deepseek-v4-flash",
    }
    if with_usage:
        body["usage"] = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_cache_hit_tokens": 2,
            "prompt_cache_miss_tokens": 8,
        }
    return httpx.Response(200, json=body)


def _client_factory(
    *, cards: list[dict[str, object]], with_usage: bool = True
) -> Callable[[str], DeepSeekClient]:
    def factory(api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return _handler(cards, with_usage=with_usage)

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    return factory


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac04_ac07.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
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
    """devices 前置 + PDF(PARSED) + 2 章节 + 牌组 + 真实加密 Key（executor 解密路径）。"""
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
            "quantity_tendency": "COMPACT",  # 3 知识点/章 × 2 章 = 6 → 2 批（batch_size 3）
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
    }


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _valid_cards(n: int = 3) -> list[dict[str, object]]:
    return [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]


def _run_to_completed(
    db_path: Path, *, cards: list[dict[str, object]], with_usage: bool = True
) -> None:
    """显式 executor 扫描一轮（mock transport）→ 任务 COMPLETED。"""
    n = scan_tasks(
        _db_factory(db_path),
        settings=_SETTINGS,
        client_factory=_client_factory(cards=cards, with_usage=with_usage),
    )
    assert n >= 1


def test_acceptance_ac04_valid_cards_inserted_and_completed(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-a：mock 返回合法卡 → 任务 COMPLETED + 合法卡入库（Schema 通过）。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    _run_to_completed(db_path, cards=_valid_cards())
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 6  # 2 批 × 3 卡，全部入库
    assert body["total_batch_count"] == 2 and body["completed_batch_count"] == 2
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 6
    assert all(c.source == "GENERATED" for c in cards)
    # AC-04-c 附证：Rubric 分数落库（仅观测），但入库与否由 Schema 决定——合法卡全部入库
    assert all(c.rubric_total_score is not None for c in cards)


def test_acceptance_ac04_invalid_cards_not_inserted_skipped(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-b：mock 返回非法卡（缺 question/answer，Schema 违约）→ 批次 SKIPPED 不入库。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    _run_to_completed(db_path, cards=[{"type": "QUESTION"}])
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"  # 批次级失败不中断任务（4.2）
    assert body["generated_card_count"] == 0
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
    assert cards == []  # 非法卡不入库（Schema 是唯一门槛）
    assert all(b.status == "SKIPPED" for b in batches)


def test_acceptance_ac04_rubric_no_auto_fix_prune_or_regenerate(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-c/d：低分但 Schema 合法的卡照常入库；不自动修复/淘汰/补生成。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    # Schema 合法（front/back/question/answer minLength=1 均满足）但 Rubric 极低
    # （evidence/correctness 1 分、difficulty 0-2、learning 2 → rubric_total_score ≤ 6）；
    # 内容互异避免 generation_item_id 防重干扰（防重属 AC-05，另测）
    low_quality: list[dict[str, object]] = [
        {"type": "QUESTION", "question": f"x{i}", "answer": f"x{i}"} for i in range(3)
    ]
    _run_to_completed(db_path, cards=low_quality)
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
    assert len(cards) == 6  # 低分合法卡全部入库（Rubric 不淘汰）
    assert len(batches) == 2
    assert all(b.status == "SUCCEEDED" for b in batches)
    assert all(b.retry_count == 0 for b in batches)  # 不因低分自动重试/修复
    assert body["total_batch_count"] == 2 and body["completed_batch_count"] == 2  # 不补生成
    assert all(
        c.evidence_score is not None
        and c.correctness_score is not None
        and c.difficulty_score is not None
        and c.learning_value_score is not None
        and c.rubric_total_score is not None
        and c.rubric_total_score <= 6
        for c in cards
    )  # 低分卡仍在牌组（不淘汰），且评分已观测


def test_acceptance_ac07_quality_and_cache_recorded(ctx: tuple[TestClient, Path]) -> None:
    """AC-07-a/b：批次列表含 Rubric/质量/Cache 记录；单卡 Rubric 分数落库。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    _run_to_completed(db_path, cards=_valid_cards())
    resp = client.get(f"/tasks/{task_id}/batches", headers=device)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    for item in items:
        assert item["status"] == "SUCCEEDED"
        # AC-07-b：Prompt Cache 命中/未命中/输出 Token 记录
        assert item["cache_hit_tokens"] == 2
        assert item["cache_miss_tokens"] == 8
        assert item["output_tokens"] == 5
        # AC-07-a：整批质量统计（仅观测，随 rubrics 落库）
        assert item["rubric_version"] == "v1"
        assert item["prompt_version"] == "v1" and item["schema_version"] == "v1"
        assert item["model"] == "deepseek-v4-flash"
        assert item["http_status"] == 200
        assert item["coverage_rate"] == 1.0
        assert item["duplicate_rate"] == 0.0
        assert isinstance(item["difficulty_distribution"], dict)
        assert isinstance(item["chapter_distribution"], dict)
        assert isinstance(item["card_type_distribution"], dict)
        assert item["difficulty_deviation"] == 0.0
        assert item["retry_count"] == 0
        assert item["cost_estimate"] is not None and item["cost_estimate"] > 0  # 仅观测
        assert isinstance(item["generated_item_ids"], list) and len(item["generated_item_ids"]) == 3
    # AC-07-a：单卡 Rubric 5 分数字段（3.9；经卡片详情核验，此处直读 DB 验证落库）
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 6
    assert all(
        c.evidence_score is not None
        and c.correctness_score is not None
        and c.difficulty_score is not None
        and c.learning_value_score is not None
        and c.rubric_total_score is not None
        and 0 < c.rubric_total_score <= 12
        for c in cards
    )


def test_acceptance_ac07_abnormal_cache_data_does_not_gate_insertion(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-07-c：Cache 数据异常（usage 缺失 → token 观测 None）不改变既定入库规则。"""
    client, db_path = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    _run_to_completed(db_path, cards=_valid_cards(), with_usage=False)
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 6  # 入库规则不受 cache 异常影响
    resp = client.get(f"/tasks/{task_id}/batches", headers=device)
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["status"] == "SUCCEEDED"
        assert item["cache_hit_tokens"] is None  # 异常时观测值 None，不抛错不阻塞
        assert item["cache_miss_tokens"] is None
        assert item["output_tokens"] is None
        assert item["cost_estimate"] is None  # 无 usage 不估算
