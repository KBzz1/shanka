"""样卡 API 集成测试（迁移 schema + HTTP）。

POST /samples 无副作用、不落库，豁免幂等键（structure-contract 1.3/6.3）——
核心断言：不带 Idempotency-Key 请求成功。种子直写迁移后 DB（FK 强制：users 前置）。
"""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.config import Settings
from app.main import create_app
from infra.db.models import Card, Chapter, PdfFile, User
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
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶（连发 >5 req/s），显式调高隔离,
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _config() -> dict[str, object]:
    return {
        "quantity_tendency": "BALANCED",
        "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
    }


def _seed_pdf(db_path: Path, *, user_id: str) -> dict[str, object]:
    """users 前置（FK 强制）+ PDF（PARSED）+ 2 章节（user 域）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用（不重复插入）
            session.add(
                User(
                    user_id=user_id,
                    username=f"u-{user_id[:8]}",
                    password_hash="x",
                    created_at="2026-08-11T00:00:00.000Z",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）——users 行先落库
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
    user = _user(client)
    seed = _seed_pdf(db_path, user_id=_user_id(db_path))
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": _config(),
        },
        headers=user,  # 无 Idempotency-Key：豁免
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


def test_samples_cross_user_404(ctx: tuple[TestClient, Path]) -> None:
    """跨用户访问他人 PDF → 404 PDF_NOT_FOUND。"""
    client, db_path = ctx
    seed = _seed_pdf(db_path, user_id=_uuid())
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": _config(),
        },
        headers=_user(client, "user2", "pass-2222"),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PDF_NOT_FOUND"


def test_samples_invalid_ratio_400(ctx: tuple[TestClient, Path]) -> None:
    """difficulty_ratio 非法（和 ≠ 1）→ 400 VALIDATION_ERROR（validate_config 统一判定）。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_pdf(db_path, user_id=_user_id(db_path))
    bad_config = _config()
    bad_config["difficulty_ratio"] = {"basic": 0.5, "understanding": 0.5, "application": 0.2}
    resp = client.post(
        "/samples",
        json={
            "file_id": seed["file_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": bad_config,
        },
        headers=user,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
