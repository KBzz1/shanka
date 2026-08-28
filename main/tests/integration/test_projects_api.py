"""学习项目 API 集成测试（V2.5；迁移 schema + HTTP）：创建/列表/详情/重命名/确认章节/
章节范围设置/替换 PDF/活跃任务保护/跨用户拒绝 + /pdfs 兼容路由委托同一业务模型。

契约锚点：structure-contract 3.16/3.17/6.2；openapi /projects 系列（2.5.0）。
测试环境无后台扫描循环：需要解析的用例显式 scan_once，或 ORM 直种 PARSED 项目。
"""

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from services.pdf.scanner import scan_once
from tests.conftest import auth_headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "projects_api.db"
    cfg = Config(str(Path(__file__).resolve().parents[3] / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,
    )
    return TestClient(create_app(settings))


def _user(
    client: TestClient, username: str = "alice", password: str = "secret-pass-1"
) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    from sqlalchemy import text

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    engine.dispose()
    assert row is not None
    return str(row)


def _pdf_bytes() -> bytes:
    return b"%PDF-1.4 fake pdf content for upload validation"


def _seed_parsed_project(
    db_path: Path,
    user_id: str,
    *,
    chapters: int = 2,
    confirmed: bool = False,
) -> dict[str, Any]:
    """ORM 直种 PARSED PDF + 章节 + 学习项目（章节相关用例；完整解析链路由验收套件覆盖）。"""
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Chapter, LearningProject, PdfFile
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    project_id, file_id = str(uuid.uuid4()), str(uuid.uuid4())
    chapter_ids = [str(uuid.uuid4()) for _ in range(chapters)]
    with factory() as session:
        session.add(
            PdfFile(
                file_id=file_id,
                user_id=user_id,
                filename="seed.pdf",
                storage_key="a" * 32,
                size_bytes=100,
                status="PARSED",
                created_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.flush()  # 无 relationship 时 UoW 不保证插入顺序——先落 pdf_files 行
        for i, cid in enumerate(chapter_ids):
            session.add(
                Chapter(
                    chapter_id=cid,
                    file_id=file_id,
                    name=f"第{i + 1}章",
                    start_page=i * 10 + 1,
                    end_page=i * 10 + 10,
                )
            )
        session.add(
            LearningProject(
                project_id=project_id,
                user_id=user_id,
                file_id=file_id,
                name="种子项目",
                chapters_confirmed_at="2026-08-15T00:00:00.000Z" if confirmed else None,
                version="2026-08-15T00:00:00.000Z",
                created_at="2026-08-15T00:00:00.000Z",
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.commit()
    engine.dispose()
    return {"project_id": project_id, "file_id": file_id, "chapter_ids": chapter_ids}


def _seed_task(
    db_path: Path,
    user_id: str,
    project_id: str,
    file_id: str,
    *,
    status: str = "COMPLETED",
    chapter_ids: list[str] | None = None,
) -> str:
    """ORM 直种任务（Task 5 前测试基座）：selected_chapters 存契约 3.4 Chapter[] 快照。"""
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Task
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    task_id = str(uuid.uuid4())
    snapshot = [
        {"chapter_id": cid, "name": "x", "start_page": 1, "end_page": 10}
        for cid in (chapter_ids or [])
    ]
    with factory() as session:
        session.add(
            Task(
                task_id=task_id,
                user_id=user_id,
                project_id=project_id,
                file_id=file_id,
                status=status,
                selected_chapters=json.dumps(snapshot, ensure_ascii=False),
                generation_config="{}",
                generated_card_count=0,
                resumable=0,
                created_at="2026-08-15T00:00:00.000Z",
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.commit()
    engine.dispose()
    return task_id


def _seed_deck(db_path: Path, user_id: str, project_id: str) -> str:
    """ORM 直种归属项目的牌组。"""
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Deck
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    deck_id = str(uuid.uuid4())
    with factory() as session:
        session.add(
            Deck(
                deck_id=deck_id,
                user_id=user_id,
                project_id=project_id,
                name="项目牌组",
                source="GENERATED",
                version="1.0",
                created_at="2026-08-15T00:00:00.000Z",
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.commit()
    engine.dispose()
    return deck_id


def _error_code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


# ---------- 上传 → 建项目 ----------


def test_projects_upload_creates_project_with_default_name(client: TestClient) -> None:
    """V25-GEN-FR-01：PDF 上传成功即建立项目；缺省名取文件名去扩展名。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["project_id"]
    assert body["name"] == "book"  # 默认名 = 上传文件名去扩展名
    assert body["status"] == "PARSING"  # 上传即建立，PDF 异步解析
    assert body["chapter_count"] == 0
    assert body["deck_count"] == 0
    assert body["task_count"] == 0
    assert body["file"]["file_id"] and body["file"]["status"] in ("PENDING", "PARSING")
    assert body["file"]["filename"] == "book.pdf"
    assert body["file"]["chapters"] is None  # 解析完成前无章节
    assert body["version"] and body["created_at"] and body["updated_at"]
    # 详情可轮询（PARSING 时 GET 200）
    resp = client.get(f"/projects/{body['project_id']}", headers=user)
    assert resp.status_code == 200
    assert resp.json()["status"] == "PARSING"


def test_projects_upload_custom_name_trimmed(client: TestClient) -> None:
    """显式 name：去首尾空白后保存。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        data={"name": "  我的 项目  "},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "我的 项目"


def test_projects_upload_blank_name_400(client: TestClient) -> None:
    """显式 name 全空白 → 400 VALIDATION_ERROR（去首尾空白后为空）。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        data={"name": "   "},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "VALIDATION_ERROR"


def test_projects_upload_name_too_long_400(client: TestClient) -> None:
    """显式 name > 60 字符 → 400 VALIDATION_ERROR。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("book.pdf", _pdf_bytes(), "application/pdf")},
        data={"name": "长" * 61},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "VALIDATION_ERROR"


def test_projects_upload_invalid_pdf_400(client: TestClient) -> None:
    """三重校验沿用 PDF 上传管线：魔数不符 → 400 PDF_UPLOAD_INVALID。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "PDF_UPLOAD_INVALID"


def test_projects_upload_requires_idempotency_key(client: TestClient) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers=user,
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "VALIDATION_ERROR"


# ---------- 列表与跨设备检索 ----------


def test_projects_list_empty_state_and_cross_user_isolation(client: TestClient) -> None:
    """真实空态 + 跨用户隔离：B 看不到 A 的项目，B 的列表为空。"""
    user_a = _user(client)
    user_b = _user(client, "user2", "pass-2222")
    assert client.get("/projects", headers=user_a).json() == {"items": []}
    resp = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user_a, **_idem()},
    )
    assert resp.status_code == 201
    items_a = client.get("/projects", headers=user_a).json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["name"] == "a"
    assert client.get("/projects", headers=user_b).json() == {"items": []}


def test_projects_list_skips_legacy_project_without_pdf_record(
    client: TestClient, tmp_path: Path
) -> None:
    """损坏历史项目不能令同用户所有有效项目列表 500。"""
    user = _user(client)
    db_path = tmp_path / "projects_api.db"
    with sqlite3.connect(db_path) as connection:
        # Raw legacy SQLite data may predate FK/NOT NULL enforcement.
        connection.execute(
            "INSERT INTO learning_projects "
            "(project_id, user_id, file_id, name, chapters_confirmed_at, version, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                _user_id(db_path),
                "missing-pdf-record",
                "legacy shell",
                "2026-08-28T00:00:00.000Z",
                "2026-08-28T00:00:00.000Z",
                "2026-08-28T00:00:00.000Z",
            ),
        )

    response = client.get("/projects", headers=user)

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_projects_list_ordered_by_updated_desc(client: TestClient) -> None:
    """列表按 updated_at 倒序（最近使用在前）；重命名刷新排序。"""
    user = _user(client)
    first = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()
    second = client.post(
        "/projects",
        files={"file": ("b.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()
    ids = [p["project_id"] for p in client.get("/projects", headers=user).json()["items"]]
    assert ids == [second["project_id"], first["project_id"]]
    # 重命名 first（刷新 updated_at）→ 排序翻转
    resp = client.patch(
        f"/projects/{first['project_id']}",
        json={"name": "改名"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    ids = [p["project_id"] for p in client.get("/projects", headers=user).json()["items"]]
    assert ids == [first["project_id"], second["project_id"]]


def test_projects_get_detail_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    """跨用户访问项目详情 → 404 PROJECT_NOT_FOUND（统一 404，不暴露存在性）。"""
    user_a = _user(client)
    project = _seed_parsed_project(
        tmp_path / "projects_api.db", _user_id(tmp_path / "projects_api.db")
    )
    user_b = _user(client, "user2", "pass-2222")
    resp = client.get(f"/projects/{project['project_id']}", headers=user_b)
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"
    # A 本人可读
    resp = client.get(f"/projects/{project['project_id']}", headers=user_a)
    assert resp.status_code == 200
    assert resp.json()["project_id"] == project["project_id"]


def test_projects_get_detail_derived_counts_and_chapters(
    client: TestClient, tmp_path: Path
) -> None:
    """详情派生计数（chapter/deck/task）+ PARSED 时 file.chapters 摘要；多任务每项目。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db), confirmed=True)
    _seed_deck(db, _user_id(db), str(project["project_id"]))
    _seed_task(db, _user_id(db), str(project["project_id"]), str(project["file_id"]))
    _seed_task(db, _user_id(db), str(project["project_id"]), str(project["file_id"]))
    body = client.get(f"/projects/{project['project_id']}", headers=user).json()
    assert body["status"] == "READY"  # PARSED + chapters_confirmed_at → READY
    assert body["chapter_count"] == len(project["chapter_ids"])
    assert body["deck_count"] == 1
    assert body["task_count"] == 2  # 一个项目可含多个任务
    assert [c["chapter_id"] for c in body["file"]["chapters"]] == project["chapter_ids"]
    # 未确认章节 → AWAITING_CHAPTER_CONFIRMATION
    project2 = _seed_parsed_project(db, _user_id(db), confirmed=False)
    body = client.get(f"/projects/{project2['project_id']}", headers=user).json()
    assert body["status"] == "AWAITING_CHAPTER_CONFIRMATION"


# ---------- 重命名 ----------


def test_projects_rename_trims_and_bumps_version(client: TestClient, tmp_path: Path) -> None:
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    before = client.get(f"/projects/{project['project_id']}", headers=user).json()
    resp = client.patch(
        f"/projects/{project['project_id']}",
        json={"name": "  新名称  "},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "新名称"
    assert body["version"] != before["version"]  # 缓存版本随写刷新
    # 可重名：两个项目同名
    resp = client.post(
        "/projects",
        files={"file": ("c.pdf", _pdf_bytes(), "application/pdf")},
        data={"name": "新名称"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "新名称"


def test_projects_rename_blank_400(client: TestClient, tmp_path: Path) -> None:
    """全空白 name → 400 VALIDATION_ERROR（pydantic 只查原始长度，服务端去空白校验兜底）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.patch(
        f"/projects/{project['project_id']}",
        json={"name": "   "},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "VALIDATION_ERROR"


def test_projects_rename_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.patch(
        f"/projects/{project['project_id']}",
        json={"name": "x"},
        headers={**user_b, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


# ---------- 确认章节 ----------


def test_projects_confirm_chapters_ready_and_reconfirm_409(
    client: TestClient, tmp_path: Path
) -> None:
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db), confirmed=False)
    resp = client.post(
        f"/projects/{project['project_id']}/confirm-chapters",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "READY"
    # 再次确认（已 READY）→ 409 PROJECT_STATE_CONFLICT
    resp = client.post(
        f"/projects/{project['project_id']}/confirm-chapters",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"


def test_projects_confirm_chapters_before_parsed_409(client: TestClient) -> None:
    """PDF 未解析完成 → 确认章节 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    project_id = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["project_id"]
    resp = client.post(
        f"/projects/{project_id}/confirm-chapters",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"


def test_projects_confirm_chapters_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.post(
        f"/projects/{project['project_id']}/confirm-chapters",
        headers={**user_b, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


# ---------- 替换 PDF ----------


def test_projects_replace_pdf_only_parse_failed(client: TestClient, tmp_path: Path) -> None:
    """仅 PARSE_FAILED 项目可替换：PARSED → 409；FAILED → 200 原子替换重新解析。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    # 1) PARSED 项目替换 → 409 PROJECT_STATE_CONFLICT
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.post(
        f"/projects/{project['project_id']}/replace-pdf",
        files={"file": ("new.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"
    # 2) 解析失败项目（HTTP 上传坏 PDF + 扫描失败）→ 可替换
    resp = client.post(
        "/projects",
        files={"file": ("bad.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    project_id, old_file_id = resp.json()["project_id"], resp.json()["file"]["file_id"]
    app = cast(Any, client.app)
    scan_once(app.state.session_factory, storage=app.state.storage)
    body = client.get(f"/projects/{project_id}", headers=user).json()
    assert body["status"] == "PARSE_FAILED"
    resp = client.post(
        f"/projects/{project_id}/replace-pdf",
        files={"file": ("good.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "bad"  # 项目名保留
    assert body["file"]["file_id"] != old_file_id  # 原子替换为新 PDF
    assert body["file"]["filename"] == "good.pdf"
    assert body["status"] == "PARSING"  # 重新解析
    # 旧 PDF 不再可读
    resp = client.get(f"/pdfs/{old_file_id}", headers=user)
    assert resp.status_code == 404


def test_projects_replace_pdf_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.post(
        f"/projects/{project['project_id']}/replace-pdf",
        files={"file": ("x.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user_b, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


# ---------- 章节 PATCH（项目路由） ----------


def test_projects_chapter_patch_and_partial_update(client: TestClient, tmp_path: Path) -> None:
    """项目路由修改章节（部分更新）：PARSED 后 200；未解析 → 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    chapter_id = project["chapter_ids"][0]
    resp = client.patch(
        f"/projects/{project['project_id']}/chapters/{chapter_id}",
        json={"name": "改名章节"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chapter_id"] == chapter_id
    assert body["name"] == "改名章节"
    assert body["start_page"] == 1 and body["end_page"] == 10  # 未提供字段保持
    # 跨用户 → 404
    user_b = _user(client, "user2", "pass-2222")
    resp = client.patch(
        f"/projects/{project['project_id']}/chapters/{chapter_id}",
        json={"name": "x"},
        headers={**user_b, **_idem()},
    )
    assert resp.status_code == 404


def test_projects_chapter_patch_not_parsed_409(client: TestClient) -> None:
    """未解析项目改章节 → 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    project_id = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["project_id"]
    resp = client.patch(
        f"/projects/{project_id}/chapters/{uuid.uuid4()}",
        json={"name": "x"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"


# ---------- 项目学习设置（章节范围） ----------


def test_projects_study_settings_defaults(client: TestClient, tmp_path: Path) -> None:
    """get-or-create 默认：空范围 + include_unassigned=false（契约 3.17）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.get(f"/projects/{project['project_id']}/study-settings", headers=user)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "selected_new_card_chapter_ids": [],
        "include_unassigned": False,
        "updated_at": body["updated_at"],
    }
    assert body["updated_at"]


def test_projects_study_settings_patch_persists_and_cross_device(
    client: TestClient, tmp_path: Path
) -> None:
    """PATCH 章节范围 → 持久化；同一账号新会话（跨设备语义）读到同一值。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    ids = [str(project["chapter_ids"][0])]
    resp = client.patch(
        f"/projects/{project['project_id']}/study-settings",
        json={"selected_new_card_chapter_ids": ids, "include_unassigned": True},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_new_card_chapter_ids"] == ids
    assert body["include_unassigned"] is True
    # 部分更新：只改 include_unassigned → 范围保持
    resp = client.patch(
        f"/projects/{project['project_id']}/study-settings",
        json={"include_unassigned": False},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    assert resp.json()["selected_new_card_chapter_ids"] == ids
    assert resp.json()["include_unassigned"] is False
    # 新会话（再次登录）跨设备一致
    resp = client.get(f"/projects/{project['project_id']}/study-settings", headers=user)
    assert resp.json()["selected_new_card_chapter_ids"] == ids


def test_projects_study_settings_foreign_chapter_404(client: TestClient, tmp_path: Path) -> None:
    """范围含不属于本项目的章节 id → 404 CHAPTER_NOT_FOUND。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.patch(
        f"/projects/{project['project_id']}/study-settings",
        json={"selected_new_card_chapter_ids": [str(uuid.uuid4())]},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "CHAPTER_NOT_FOUND"


def test_projects_study_settings_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.get(f"/projects/{project['project_id']}/study-settings", headers=user_b)
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


# ---------- 删除保护（活跃任务 / 状态） ----------


def test_projects_delete_parsing_state_conflict(client: TestClient) -> None:
    """解析中项目不可删除（PRD V25-GEN-FR-09）→ 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    project_id = client.post(
        "/projects",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    ).json()["project_id"]
    resp = client.delete(f"/projects/{project_id}?retain_decks=true", headers={**user, **_idem()})
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"
    # 项目仍在
    assert client.get(f"/projects/{project_id}", headers=user).status_code == 200


def test_projects_delete_active_task_conflict(client: TestClient, tmp_path: Path) -> None:
    """存在活跃（非终态）任务 → 409 PROJECT_HAS_ACTIVE_TASK。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    _seed_task(
        db,
        _user_id(db),
        str(project["project_id"]),
        str(project["file_id"]),
        status="DRAFT",
        chapter_ids=[str(project["chapter_ids"][0])],
    )
    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=false", headers={**user, **_idem()}
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_HAS_ACTIVE_TASK"


def test_projects_delete_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=true", headers={**user_b, **_idem()}
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


# ---------- /pdfs 兼容路由委托同一业务模型 ----------


def test_pdfs_compat_upload_creates_project(client: TestClient) -> None:
    """兼容 /pdfs POST 委托项目创建：上传同时建立项目（同一业务模型，无第二语义路径）。"""
    user = _user(client)
    resp = client.post(
        "/pdfs",
        files={"file": ("compat.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    items = client.get("/projects", headers=user).json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "compat"  # 默认名 = 文件名去扩展名
    assert items[0]["file"]["file_id"] == resp.json()["file_id"]


def test_pdfs_compat_delete_delegates_keep_decks(client: TestClient, tmp_path: Path) -> None:
    """兼容 /pdfs DELETE 委托项目删除（retain_decks=true）：项目/PDF 删除，牌组保留脱离项目。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    user_id = _user_id(db)
    # ORM 直种 PARSED 项目（storage 对象同步写盘，供删除清理断言）
    project = _seed_parsed_project(db, user_id)
    deck_id = _seed_deck(db, user_id, str(project["project_id"]))
    from sqlalchemy import text

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        conn.execute(
            text("UPDATE pdf_files SET storage_key = :k WHERE file_id = :f"),
            {"k": "b" * 32, "f": str(project["file_id"])},
        )
        conn.commit()
    engine.dispose()
    storage = cast(Any, client.app).state.storage
    obj = storage.open("b" * 32)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"%PDF-1.4")
    assert obj.exists()

    resp = client.delete(f"/pdfs/{project['file_id']}", headers={**user, **_idem()})
    assert resp.status_code == 204, resp.text
    # 项目与 PDF 已删
    resp = client.get(f"/projects/{project['project_id']}", headers=user)
    assert resp.status_code == 404
    assert not obj.exists()  # 存储对象随删除清理
    # 牌组保留且脱离项目（project_id 置空）
    engine = create_db_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        pid = conn.execute(
            text("SELECT project_id FROM decks WHERE deck_id = :d"), {"d": deck_id}
        ).scalar()
    engine.dispose()
    assert pid is None
