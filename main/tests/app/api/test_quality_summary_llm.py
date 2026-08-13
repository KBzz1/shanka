"""quality-summary 新口径 API 测试（LLM 升级 T12；spec §8 权威口径）。

真实 SQLite（迁移后 schema）+ TestClient；直接种子 Device/PdfFile/Deck/Task/
KnowledgePoint/Batch/Card（不经执行器）——聚焦聚合语义：

- 各评分均分只以对应字段非 NULL 的卡为分母（NULL 不计 0 分）；
- eligible_card_count = 经批次 generated_item_ids 归属的卡数；scored_card_count =
  rubric_total_score 非 NULL（"已被评分"）；sampling_rate = scored/eligible（eligible
  为 0 → JSON null）；
- difficulty 分组键 = Batch.generation_unit_id → KnowledgePoint.target_difficulty
  （SKIPPED 无卡批次按单元锚定进组，coverage=0 计入 coverage_avg）；
- 分组按批次 rubric_version 拆子组，响应含 rubric_version 字段；
- cost_estimate 带 scope == "generation-stage-only"（Batch token 列为生成阶段投影，
  不引入账本双计）。
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from infra.db.models import Batch, Card, Device, KnowledgePoint, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from services.decks.service import create_deck

REPO_ROOT = Path(__file__).resolve().parents[4]  # tests/app/api/ → 仓库根


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient + DB 路径（test_observability 同款定式）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "qs.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,
        rate_limit_ip_per_second=100,
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _session(db_path: Path) -> Session:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))()


def _seed_base(db_path: Path, *, device_id: str) -> tuple[str, str]:
    """devices + PdfFile + 牌组 → (file_id, deck_id)。"""
    with _session(db_path) as session:
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
        session.commit()
        return pdf.file_id, deck.deck_id


def _seed_task(db_path: Path, *, device_id: str, file_id: str) -> str:
    """COMPLETED 任务（观测窗口内；聚合只读任务状态）。"""
    task_id = _uuid()
    with _session(db_path) as session:
        session.add(
            Task(
                task_id=task_id,
                device_id=device_id,
                file_id=file_id,
                status="COMPLETED",
                selected_chapters="[]",
                generation_config="{}",
            )
        )
        session.commit()
    return task_id


def _seed_unit(
    db_path: Path, *, task_id: str, target_difficulty: str, topic: str = "知识点"
) -> str:
    """KnowledgePoint（target_difficulty = 规划锚定；difficulty 分组键来源）。"""
    unit_id = _uuid()
    with _session(db_path) as session:
        session.add(
            KnowledgePoint(
                knowledge_point_id=unit_id,
                task_id=task_id,
                source_chunk_id="c1",
                topic=topic,
                priority=1,
                status="PROCESSED",
                target_difficulty=target_difficulty,
                card_type="QUESTION",
            )
        )
        session.commit()
    return unit_id


def _seed_batch(
    db_path: Path,
    *,
    task_id: str,
    unit_id: str | None = None,
    status: str = "SUCCEEDED",
    rubric_version: str | None = "v2",
    item_ids: list[str] | None = None,
    coverage_rate: float | None = 1.0,
    duplicate_rate: float | None = 0.0,
) -> str:
    """Batch 行（1 单元 1 批；item_ids = generated_item_ids JSON）。"""
    batch_id = _uuid()
    with _session(db_path) as session:
        session.add(
            Batch(
                batch_id=batch_id,
                task_id=task_id,
                batch_index=1,
                status=status,
                generated_item_ids=json.dumps(item_ids or [], ensure_ascii=False),
                generation_unit_id=unit_id,
                coverage_rate=coverage_rate,
                duplicate_rate=duplicate_rate,
                model="deepseek-v4-flash",
                rubric_version=rubric_version,
                created_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
    return batch_id


def _seed_card(
    db_path: Path,
    *,
    deck_id: str,
    device_id: str,
    item_id: str,
    position: int,
    evidence: int | None = None,
    correctness: int | None = None,
    difficulty: int | None = None,
    learning: int | None = None,
    total: int | None = None,
) -> str:
    """Card 行（评分 5 字段显式可 NULL）。"""
    card_id = _uuid()
    with _session(db_path) as session:
        session.add(
            Card(
                card_id=card_id,
                deck_id=deck_id,
                device_id=device_id,
                source="GENERATED",
                position=position,
                front=f"front-{position}",
                back=f"back-{position}",
                card_type="QUESTION",
                generation_item_id=item_id,
                evidence_score=evidence,
                correctness_score=correctness,
                difficulty_score=difficulty,
                learning_value_score=learning,
                rubric_total_score=total,
                version="v1",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()
    return card_id


def test_summary_null_scores_not_zero(ctx: tuple[TestClient, Path]) -> None:
    """NULL 评分不计 0 分：evidence 分母 = 非 NULL 卡数（1 而非 2）；
    scored_card_count 按 rubric_total_score 非 NULL（1），eligible = 2。"""
    client, db_path = ctx
    device = _device()
    file_id, deck_id = _seed_base(db_path, device_id=device["X-Device-ID"])
    task_id = _seed_task(db_path, device_id=device["X-Device-ID"], file_id=file_id)
    _seed_batch(db_path, task_id=task_id, item_ids=["item-a", "item-b"])
    _seed_card(
        db_path,
        deck_id=deck_id,
        device_id=device["X-Device-ID"],
        item_id="item-a",
        position=1,
        evidence=2,
        correctness=1,
        difficulty=1,
        learning=1,
        total=5,
    )
    _seed_card(
        db_path, deck_id=deck_id, device_id=device["X-Device-ID"], item_id="item-b", position=2
    )

    resp = client.get("/observability/quality-summary", headers=device)
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["evidence_avg"] == 2.0  # 2/1（NULL 卡不计分母、不计 0 分）
    assert group["correctness_avg"] == 1.0  # 1/1（同款独立分母）
    assert group["card_count"] == 2
    assert group["eligible_card_count"] == 2
    assert group["scored_card_count"] == 1
    assert group["sampling_rate"] == 0.5


def test_summary_difficulty_group_by_unit(ctx: tuple[TestClient, Path]) -> None:
    """difficulty 分组键 = generation_unit_id → 单元 target_difficulty；
    SKIPPED 无卡批次进 BASIC 组，coverage=0 计入 coverage_avg。"""
    client, db_path = ctx
    device = _device()
    file_id, _deck_id = _seed_base(db_path, device_id=device["X-Device-ID"])
    task_id = _seed_task(db_path, device_id=device["X-Device-ID"], file_id=file_id)
    unit_id = _seed_unit(db_path, task_id=task_id, target_difficulty="BASIC")
    _seed_batch(
        db_path,
        task_id=task_id,
        unit_id=unit_id,
        status="SKIPPED",
        rubric_version=None,
        item_ids=[],
        coverage_rate=0.0,
    )

    resp = client.get(
        "/observability/quality-summary", params={"group_by": "difficulty"}, headers=device
    )
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    basic = [g for g in groups if g["key"] == "BASIC"]
    assert basic, f"SKIPPED 批次应经单元锚定进 BASIC 组：{groups}"
    assert basic[0]["coverage_avg"] == 0.0  # 无卡批次的 0 计入分母
    assert basic[0]["eligible_card_count"] == 0
    assert basic[0]["sampling_rate"] is None  # 分母 0 → JSON null


def test_summary_sampling_rate_and_rubric_version(ctx: tuple[TestClient, Path]) -> None:
    """响应含 eligible/scored/sampling_rate（0.5）与 rubric_version；
    cost_estimate 标注 scope == generation-stage-only。"""
    client, db_path = ctx
    device = _device()
    file_id, deck_id = _seed_base(db_path, device_id=device["X-Device-ID"])
    task_id = _seed_task(db_path, device_id=device["X-Device-ID"], file_id=file_id)
    _seed_batch(db_path, task_id=task_id, rubric_version="v2", item_ids=["item-a", "item-b"])
    _seed_card(
        db_path,
        deck_id=deck_id,
        device_id=device["X-Device-ID"],
        item_id="item-a",
        position=1,
        evidence=2,
        correctness=1,
        difficulty=1,
        learning=1,
        total=5,
    )
    _seed_card(
        db_path, deck_id=deck_id, device_id=device["X-Device-ID"], item_id="item-b", position=2
    )

    resp = client.get("/observability/quality-summary", headers=device)
    assert resp.status_code == 200
    summary = resp.json()
    groups = summary["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["rubric_version"] == "v2"  # 卡经批次归属 → 批次 rubric_version
    assert group["eligible_card_count"] == 2
    assert group["scored_card_count"] == 1
    assert group["sampling_rate"] == 0.5
    assert group["cost_estimate"]["scope"] == "generation-stage-only"
