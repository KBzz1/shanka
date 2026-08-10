"""验收测试：AC-03 样卡（PRD 5.6/AC-03；迁移 schema + HTTP + 种 DB 行）。

映射（PRD AC-03 四条）：
- AC-03-a 每次生成固定输出 3 张样卡 → len(sample_cards) == 3
- AC-03-b 三档难度各有 1 张 → target_difficulty == {BASIC, UNDERSTANDING, APPLICATION}
- AC-03-c 2 张普通问答 + 1 张判断题 → card_type 计 QUESTION==2 / TRUE_FALSE==1
- AC-03-d 不能编辑且不入牌组不统计 → 样卡不落库（cards/review_states 无行）：
  无持久化即无编辑入口（"不能编辑"由结构保证）；正式统计只读库内卡片，无卡片自然不统计。

PDF 上下文（plan Task 5 决策）：种 DB 行（Device + PdfFile PARSED + Chapters），
不依赖样书——AC-03 聚焦样卡构成，PDF 全链路由 AC-01 覆盖。
POST /samples 豁免 Idempotency-Key（契约 1.3 唯一豁免），请求不带幂等键。
"""

import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from infra.db.models import Card, Chapter, Device, PdfFile, ReviewState

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac03.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _config() -> dict[str, object]:
    return {
        "quantity_tendency": "BALANCED",
        "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
    }


def _seed_pdf_context(client: TestClient, device: dict[str, str]) -> tuple[str, list[str]]:
    """种 DB 行：Device（FK 前置）+ PdfFile(PARSED) + 2 章节（plan Task 5 决策，避免样书依赖）。"""
    app = cast(FastAPI, client.app)
    factory = app.state.session_factory
    with factory() as session:
        session.add(Device(device_id=device["X-Device-ID"], created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        pdf = PdfFile(
            file_id=str(uuid.uuid4()),
            device_id=device["X-Device-ID"],
            filename="b.pdf",
            storage_key=str(uuid.uuid4()),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        chapter_ids: list[str] = []
        for i in range(2):
            ch = Chapter(
                chapter_id=str(uuid.uuid4()),
                file_id=pdf.file_id,
                name=f"第{i + 1}章",
                start_page=i + 1,
                end_page=i + 2,
            )
            session.add(ch)
            session.flush()
            chapter_ids.append(ch.chapter_id)
        session.commit()
    return pdf.file_id, chapter_ids


def test_acceptance_ac03_sample_cards(client: TestClient) -> None:
    """AC-03：3 张样卡（三档难度各 1；2 问答 + 1 判断）；不入牌组不统计。"""
    device = _device()
    file_id, chapter_ids = _seed_pdf_context(client, device)
    resp = client.post(
        "/samples",
        json={
            "file_id": file_id,
            "chapter_ids": chapter_ids,
            "generation_config": _config(),
        },
        headers=device,  # 无 Idempotency-Key：幂等豁免（契约 1.3）
    )
    assert resp.status_code == 200
    cards = resp.json()["sample_cards"]
    # AC-03-a：固定 3 张
    assert len(cards) == 3
    # AC-03-b：三档难度各有 1 张
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "UNDERSTANDING", "APPLICATION"}
    # AC-03-c：2 张普通问答 + 1 张判断题
    assert sum(1 for c in cards if c["card_type"] == "QUESTION") == 2
    assert sum(1 for c in cards if c["card_type"] == "TRUE_FALSE") == 1
    # openapi Card required 字段齐全（F-2 fix：handler 合成 deck_id/position/created_at/updated_at）
    for c in cards:
        assert {"deck_id", "position", "created_at", "updated_at"} <= set(c)
    # AC-03-d：不入牌组不统计——样卡不落库（cards/review_states 无行），统计自然不含样卡
    app = cast(FastAPI, client.app)
    with app.state.session_factory() as session:
        assert session.scalar(select(Card).limit(1)) is None
        assert session.scalar(select(ReviewState).limit(1)) is None
