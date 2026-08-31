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


def _user(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _create_project_with_pdf(
    client: TestClient,
    user: dict[str, str],
    *,
    filename: str = "book.pdf",
    data: bytes | None = None,
) -> str:
    """两步创建（V25-D-29）：POST /projects + materials/pdf，返回 project_id。"""
    resp = client.post("/projects", json={"name": "验收项目"}, headers={**user, **_idem()})
    assert resp.status_code == 201, resp.text
    project_id = str(resp.json()["project_id"])
    payload = data if data is not None else SAMPLE.read_bytes()
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": (filename, payload, "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    return project_id


def test_acceptance_ac01_sample_book_parses_to_chapters(client: TestClient, tmp_path: Path) -> None:
    """AC-01-1：可提取文本层 + 可识别目录的 PDF 进入章节确认流程（PARSED + 章节列表）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _user(client)
    project_id = _create_project_with_pdf(client, device)
    _scan(client)
    resp = client.get(f"/projects/{project_id}", headers=device)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AWAITING_CHAPTER_CONFIRMATION"
    assert body["materials"][0]["status"] == "PARSED"
    assert body["materials"][0]["error_code"] is None
    chapters = body["chapters"]
    assert chapters and len(chapters) >= 3
    first = chapters[0]
    assert first["name"] and first["start_page"] >= 1 and first["end_page"] >= first["start_page"]
    assert first["material_id"] == body["materials"][0]["material_id"]


def test_acceptance_ac01_no_toc_stops_flow(client: TestClient) -> None:
    """AC-01-2：无可用目录 → FAILED + PDF_TOC_MISSING（流程停止；项目转 PARSE_FAILED）。"""
    device = _user(client)
    project_id = _create_project_with_pdf(
        client, device, filename="notoc.pdf", data=b"%PDF-1.4 broken"
    )
    _scan(client)
    resp = client.get(f"/projects/{project_id}/materials", headers=device)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["status"] == "FAILED"
    assert item["error_code"] in ("PDF_PARSE_FAILED", "PDF_TOC_MISSING")
    assert client.get(f"/projects/{project_id}", headers=device).json()["status"] == "PARSE_FAILED"


def test_acceptance_ac02_chapter_patch(client: TestClient, tmp_path: Path) -> None:
    """AC-02-1：修改章节名称（PARSED 后；部分更新——未提供字段保持不变）。"""
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    device = _user(client)
    project_id = _create_project_with_pdf(client, device)
    _scan(client)
    project = client.get(f"/projects/{project_id}", headers=device).json()
    ch = project["chapters"][0]
    resp = client.patch(
        f"/projects/{project_id}/chapters/{ch['chapter_id']}",
        json={"name": "第一章 修订"},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "第一章 修订"
    # 部分更新语义（fix round 1）：未提供的 start_page/end_page 保持不变
    assert resp.json()["start_page"] == ch["start_page"]
    assert resp.json()["end_page"] == ch["end_page"]
