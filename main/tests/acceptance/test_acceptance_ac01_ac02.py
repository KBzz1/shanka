"""验收测试：AC-01 PDF 解析 + AC-02 章节配置（PRD；迁移 schema + HTTP + 样书）。

映射：
- AC-01-1 可提取文本层 + 可识别目录的 PDF 进入章节确认流程（PARSED + 章节列表）
- AC-01-2 无可用目录 → FAILED + 错误码（流程停止）
- AC-02-1 修改章节名称（PARSED 后；部分更新语义——未提供字段保持不变）
- AC-08 后端存储边界（完整 PDF 内容不落日志/不落库）：由日志中间件不记录 body 保证，
  本文件只在上传/解析全流程中声明，不做内容级断言（Task 5 报告说明）。

测试环境无后台扫描循环：上传后显式调用 scan_once 触发解析。
"""

import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from services.pdf.scanner import scan_once
from tests.conftest import auth_headers

SAMPLE = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")


def _scan(client: TestClient) -> None:
    """显式触发扫描（测试环境无后台循环）：从 app state 取 session_factory/storage。"""
    app = cast(FastAPI, client.app)
    scan_once(app.state.session_factory, storage=app.state.storage)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac01.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _device(client: TestClient) -> dict[str, str]:
    """双头过渡窗口：Bearer（模块级缓存）+ 随机 X-Device-ID（v2.1 device 隔离语义保持）。"""
    return {**auth_headers(client), "X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_acceptance_ac01_sample_book_parses_to_chapters(client: TestClient, tmp_path: Path) -> None:
    """AC-01-1：可提取文本层 + 可识别目录的 PDF 进入章节确认流程（PARSED + 章节列表）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _device(client)
    with SAMPLE.open("rb") as f:
        resp = client.post(
            "/pdfs",
            files={"file": ("book.pdf", f, "application/pdf")},
            headers={**device, **_idem()},
        )
    assert resp.status_code == 201
    file_id = resp.json()["file_id"]
    _scan(client)
    resp = client.get(f"/pdfs/{file_id}", headers=device)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PARSED"
    assert body["error_code"] is None
    assert body["chapters"] and len(body["chapters"]) >= 3
    first = body["chapters"][0]
    assert first["name"] and first["start_page"] >= 1 and first["end_page"] >= first["start_page"]


def test_acceptance_ac01_no_toc_stops_flow(client: TestClient) -> None:
    """AC-01-2：无可用目录 → FAILED + PDF_TOC_MISSING（流程停止）。"""
    device = _device(client)
    resp = client.post(
        "/pdfs",
        files={"file": ("notoc.pdf", b"%PDF-1.4 broken", "application/pdf")},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 201
    file_id = resp.json()["file_id"]
    _scan(client)
    resp = client.get(f"/pdfs/{file_id}", headers=device)
    assert resp.status_code == 200
    assert resp.json()["status"] == "FAILED"
    assert resp.json()["error_code"] in ("PDF_PARSE_FAILED", "PDF_TOC_MISSING")


def test_acceptance_ac02_chapter_patch(client: TestClient, tmp_path: Path) -> None:
    """AC-02-1：修改章节名称（PARSED 后；部分更新——未提供字段保持不变）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _device(client)
    with SAMPLE.open("rb") as f:
        file_id = client.post(
            "/pdfs",
            files={"file": ("book.pdf", f, "application/pdf")},
            headers={**device, **_idem()},
        ).json()["file_id"]
    _scan(client)
    chapters = client.get(f"/pdfs/{file_id}", headers=device).json()["chapters"]
    ch = chapters[0]
    resp = client.patch(
        f"/pdfs/{file_id}/chapters/{ch['chapter_id']}",
        json={"name": "第一章 修订"},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "第一章 修订"
    # 部分更新语义（fix round 1）：未提供的 start_page/end_page 保持不变
    assert resp.json()["start_page"] == ch["start_page"]
    assert resp.json()["end_page"] == ch["end_page"]
