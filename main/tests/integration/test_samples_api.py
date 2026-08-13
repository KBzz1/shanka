"""样卡 API 集成测试（迁移 schema + HTTP）。

POST /samples 无副作用、不落库，豁免幂等键（structure-contract 1.3/6.3）——
核心断言：不带 Idempotency-Key 请求成功。种子直写迁移后 DB（FK 强制：devices 前置）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from infra.db.models import Card, Chapter, Device, PdfFile
from infra.db.session import create_db_engine, create_session_factory
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient + DB 路径（种子经独立 engine 直写同文件）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "samples_api.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # 双头窗口：Bearer 注册请求计入 IP 维度（连发 >5 req/s），显式调高隔离,
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _device(client: TestClient) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ 随机 X-Device-ID（v2.1 device 隔离语义保持）。"""
    return {**auth_headers(client), "X-Device-ID": str(uuid.uuid4())}


def _config() -> dict[str, object]:
    return {
        "quantity_tendency": "BALANCED",
        "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
    }


def _seed_pdf(db_path: Path, *, device_id: str) -> dict[str, object]:
    """devices 前置（FK 强制）+ PDF（PARSED）+ 2 章节。"""
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
        session.commit()
    return {"file_id": pdf.file_id, "chapter_ids": chapter_ids}


def test_samples_post_three_cards_without_idempotency_key(
    ctx: tuple[TestClient, Path],
) -> None:
    """合法请求：不带 Idempotency-Key 成功（幂等豁免）；3 张样卡构成正确且不入库。"""
    client, db_path = ctx
    device = _device(client)
    seed = _seed_pdf(db_path, device_id=device["X-Device-ID"])
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": _config(),
        },
        headers=device,  # 无 Idempotency-Key：豁免
    )
    assert resp.status_code == 200
    cards = resp.json()["sample_cards"]
    assert len(cards) == 3
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "UNDERSTANDING", "APPLICATION"}
    assert sum(1 for c in cards if c["card_type"] == "QUESTION") == 2
    assert sum(1 for c in cards if c["card_type"] == "TRUE_FALSE") == 1
    # R-14：SampleCard 轻量组件（structure-contract 3.13）——无落库/归属/版本占位字段
    for c in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(c)
        assert {"deck_id", "position", "created_at", "updated_at"} & set(c) == set()
    # 样卡不入库
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        assert session.scalar(select(Card).limit(1)) is None


def test_samples_cross_device_404(ctx: tuple[TestClient, Path]) -> None:
    """跨设备访问他人 PDF → 404 PDF_NOT_FOUND。"""
    client, db_path = ctx
    seed = _seed_pdf(db_path, device_id=_uuid())
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": _config(),
        },
        headers=_device(client),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PDF_NOT_FOUND"


def test_samples_invalid_ratio_400(ctx: tuple[TestClient, Path]) -> None:
    """difficulty_ratio 非法（和 ≠ 1）→ 400 VALIDATION_ERROR（validate_config 统一判定）。"""
    client, db_path = ctx
    device = _device(client)
    seed = _seed_pdf(db_path, device_id=device["X-Device-ID"])
    bad_config = _config()
    bad_config["difficulty_ratio"] = {"basic": 0.5, "understanding": 0.5, "application": 0.2}
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": bad_config,
        },
        headers=device,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
