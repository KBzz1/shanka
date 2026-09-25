"""Markdown 资料端到端集成测试（V25-D-40：上传 → 同步解析 → 章节 → 项目状态）。

基座同 AC-01（alembic 建库 + TestClient）：Markdown 同步解析即时就绪，
不经过 PENDING/PARSING 异步态。
"""

import uuid
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


def _client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "markdown-api.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _md(body: str) -> bytes:
    return ("---\ntitle: 笔记\n---\n\n" + body).encode("utf-8")


def _upload(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    data: bytes,
    name: str = "notes.md",
    idempotency_key: str | None = None,
) -> Any:
    return client.post(
        f"/projects/{project_id}/materials/markdown",
        files={"file": (name, data, "text/markdown")},
        headers={"Idempotency-Key": idempotency_key or _idem()["Idempotency-Key"], **headers},
    )


def test_markdown_upload_headings_to_chapters(tmp_path: Path) -> None:
    """超阈值 + 标题结构 → HEADING 章节、READY、项目进入章节确认。"""
    client = _client(tmp_path)
    headers = auth_headers(client)
    project_id = str(
        client.post("/projects", json={"name": "P"}, headers={**headers, **_idem()}).json()[
            "project_id"
        ]
    )
    filler = "内容。" * 3000  # 每章约 9000 字 ×3 > 24k 阈值
    data = _md(f"## 一 开场\n\n{filler}\n\n## 二 进阶\n\n{filler}\n\n## 三 冲刺\n\n{filler}\n")
    key = _idem()["Idempotency-Key"]
    resp = _upload(client, headers, project_id, data, idempotency_key=key)
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["type"] == "MARKDOWN" and item["status"] == "READY"
    body = client.get(f"/projects/{project_id}", headers=headers).json()
    chapters = body["chapters"]
    assert [c["name"] for c in chapters] == ["一 开场", "二 进阶", "三 冲刺"]
    assert all(c["source"] == "HEADING" for c in chapters)
    assert body["status"] == "AWAITING_CHAPTER_CONFIRMATION"
    # 幂等重放：同键同体 → 同结果，不重复落资料
    replay = _upload(client, headers, project_id, data, idempotency_key=key)
    assert replay.status_code == 201
    assert replay.json()["material_id"] == item["material_id"]
    assert (
        len(client.get(f"/projects/{project_id}/materials", headers=headers).json()["items"]) == 1
    )


def test_markdown_upload_small_is_auto_single_chapter(tmp_path: Path) -> None:
    """小 md（≤阈值）→ AUTO 恒单章（分诊规则 2，零模型）。"""
    client = _client(tmp_path)
    headers = auth_headers(client)
    project_id = str(
        client.post("/projects", json={"name": "P"}, headers={**headers, **_idem()}).json()[
            "project_id"
        ]
    )
    resp = _upload(client, headers, project_id, _md("## 一\n\n少量内容\n\n## 二\n\n少量内容\n"))
    assert resp.status_code == 201
    chapters = client.get(f"/projects/{project_id}", headers=headers).json()["chapters"]
    assert len(chapters) == 1
    assert chapters[0]["source"] == "AUTO"
    assert chapters[0]["start_page"] == 1 and chapters[0]["end_page"] >= 1


def test_markdown_upload_invalid_rejected(tmp_path: Path) -> None:
    """扩展名/MIME 不符 → 400 MARKDOWN_UPLOAD_INVALID，不落资料行。"""
    client = _client(tmp_path)
    headers = auth_headers(client)
    project_id = str(
        client.post("/projects", json={"name": "P"}, headers={**headers, **_idem()}).json()[
            "project_id"
        ]
    )
    resp = client.post(
        f"/projects/{project_id}/materials/markdown",
        files={"file": ("a.txt", b"plain text", "text/markdown")},
        headers={**headers, **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "MARKDOWN_UPLOAD_INVALID"
    assert client.get(f"/projects/{project_id}/materials", headers=headers).json()["items"] == []
