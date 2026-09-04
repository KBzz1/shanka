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

    from infra.db.models import Chapter, LearningProject, Material, PdfFile
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    project_id, file_id = str(uuid.uuid4()), str(uuid.uuid4())
    chapter_ids = [str(uuid.uuid4()) for _ in range(chapters)]
    with factory() as session:
        session.add(
            LearningProject(
                project_id=project_id,
                user_id=user_id,
                name="种子项目",
                chapters_confirmed_at="2026-08-15T00:00:00.000Z" if confirmed else None,
                version="2026-08-15T00:00:00.000Z",
                created_at="2026-08-15T00:00:00.000Z",
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.flush()
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
        session.flush()
        session.add(
            Material(
                material_id=file_id,  # PDF 资料 material_id == file_id（契约 3.2a）
                project_id=project_id,
                type="PDF",
                name="seed.pdf",
                status=None,
                size_bytes=100,
                created_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.flush()
        for i, cid in enumerate(chapter_ids):
            session.add(
                Chapter(
                    chapter_id=cid,
                    file_id=file_id,
                    material_id=file_id,
                    name=f"第{i + 1}章",
                    start_page=i * 10 + 1,
                    end_page=i * 10 + 10,
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
        {
            "chapter_id": cid,
            "material_id": file_id,
            "name": "x",
            "start_page": 1,
            "end_page": 10,
        }
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


# ---------- 两步创建 + 资料添加（V25-D-29/31/32）----------


def _create_project(client: TestClient, user: dict[str, str], name: str = "新书项目") -> Any:
    resp = client.post(
        "/projects",
        json={"name": name},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_pdf_material(
    client: TestClient, user: dict[str, str], project_id: str, filename: str = "book.pdf"
) -> Any:
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": (filename, _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_projects_create_empty_then_add_pdf_parses(client: TestClient) -> None:
    """V25-D-29 两步创建：空项目(EMPTY) → 添加 PDF(PARSING) → 详情轮询。"""
    user = _user(client)
    body = _create_project(client, user, "  我的项目  ")
    assert body["name"] == "我的项目"
    assert body["status"] == "EMPTY"  # 空项目存活
    assert body["materials"] == []
    assert body["chapter_count"] == 0
    material = _add_pdf_material(client, user, body["project_id"])
    assert material["type"] == "PDF"
    assert material["status"] in ("PENDING", "PARSING")
    assert material["project_id"] == body["project_id"]
    detail = client.get(f"/projects/{body['project_id']}", headers=user).json()
    assert detail["status"] == "PARSING"
    assert len(detail["materials"]) == 1
    assert detail["materials"][0]["material_id"] == material["material_id"]


def test_projects_add_text_material_ready_single_chapter(client: TestClient) -> None:
    """V25-D-32：粘贴文本即时就绪；单章节页码 null；项目转 AWAITING_CHAPTER_CONFIRMATION。"""
    user = _user(client)
    body = _create_project(client, user)
    resp = client.post(
        f"/projects/{body['project_id']}/materials/text",
        json={"name": "课堂笔记", "content": "第一段内容。\n\n第二段内容，主题不同。"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    material = resp.json()
    assert material["type"] == "TEXT"
    assert material["status"] == "READY"
    assert material["chapter"] is not None
    assert material["chapter"]["material_id"] == material["material_id"]
    assert material["chapter"]["start_page"] is None
    detail = client.get(f"/projects/{body['project_id']}", headers=user).json()
    assert detail["status"] == "AWAITING_CHAPTER_CONFIRMATION"
    assert detail["chapter_count"] == 1


def test_projects_text_material_too_long_400(client: TestClient) -> None:
    """V25-D-32：>30000 字拒绝（全局校验失败统一 400）。"""
    user = _user(client)
    body = _create_project(client, user)
    resp = client.post(
        f"/projects/{body['project_id']}/materials/text",
        json={"name": "超长", "content": "字" * 30001},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 400
    assert _error_code(resp) == "VALIDATION_ERROR"


def test_projects_material_add_resets_confirmation(client: TestClient, tmp_path: Path) -> None:
    """V25-D-31：新增资料重置章节确认（READY → AWAITING；TEXT 资料即时就绪可观测）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    uid = _user_id(db)
    seed = _seed_parsed_project(db, uid, confirmed=True)
    pid = seed["project_id"]
    assert client.get(f"/projects/{pid}", headers=user).json()["status"] == "READY"
    resp = client.post(
        f"/projects/{pid}/materials/text",
        json={"name": "补充笔记", "content": "新资料重置确认。"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    assert (
        client.get(f"/projects/{pid}", headers=user).json()["status"]
        == "AWAITING_CHAPTER_CONFIRMATION"
    )


def test_projects_delete_material_three_tiers_and_empty_alive(
    client: TestClient, tmp_path: Path
) -> None:
    """V25-D-30/29：资料删除 → 空项目存活；retain_cards 决定卡去留（计数经 preflight 观测）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    uid = _user_id(db)
    seed = _seed_parsed_project(db, uid, confirmed=True)
    pid, mid = seed["project_id"], seed["file_id"]
    # 删除 PDF 资料（无卡场景）→ 项目转 EMPTY 仍存活
    resp = client.delete(
        f"/projects/{pid}/materials/{mid}?retain_cards=true",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "EMPTY"
    assert body["materials"] == []
    # 空项目可再添加文本资料复活
    resp = client.post(
        f"/projects/{pid}/materials/text",
        json={"name": "新笔记", "content": "复活内容。"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    assert (
        client.get(f"/projects/{pid}", headers=user).json()["status"]
        == "AWAITING_CHAPTER_CONFIRMATION"
    )


def test_projects_material_list_and_cross_project_404(client: TestClient, tmp_path: Path) -> None:
    user = _user(client)
    db = tmp_path / "projects_api.db"
    uid = _user_id(db)
    seed = _seed_parsed_project(db, uid)
    resp = client.get(f"/projects/{seed['project_id']}/materials", headers=user)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1 and items[0]["type"] == "PDF"
    # 他人项目资料删除 → 404
    other = _user(client, username="bob", password="secret-pass-2")
    resp = client.delete(
        f"/projects/{seed['project_id']}/materials/{seed['file_id']}",
        headers={**other, **_idem()},
    )
    assert resp.status_code == 404


def _seed_preflight_cards(
    db_path: Path, user_id: str, deck_id: str, chapter_ids: list[str]
) -> None:
    """预检计数口径种子：可见 2 张（第 1 章）+ STAGED 1 张 + 已入删除批次 1 张。"""
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.models import Card, CardDeletionBatch
    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    batch_id = str(uuid.uuid4())
    now = "2026-09-04T00:00:00.000Z"

    def _card(position: int, chapter_id: str, **extra: Any) -> Card:
        return Card(
            card_id=str(uuid.uuid4()),
            deck_id=deck_id,
            user_id=user_id,
            source="GENERATED",
            position=position,
            front=f"卡{position}",
            back="背",
            card_type="QUESTION",
            chapter_id=chapter_id,
            version=now,
            created_at=now,
            updated_at=now,
            **extra,
        )

    with factory() as session:
        session.add(
            CardDeletionBatch(
                delete_batch_id=batch_id,
                user_id=user_id,
                status="PENDING",
                undo_until=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add_all(
            [
                _card(1, chapter_ids[0]),
                _card(2, chapter_ids[0]),
                _card(3, chapter_ids[0], publication_state="STAGED"),
                _card(4, chapter_ids[0], delete_batch_id=batch_id),
            ]
        )
        session.commit()
    engine.dispose()


def test_projects_material_deletion_preflight_counts_and_silent_cancel(
    client: TestClient, tmp_path: Path
) -> None:
    """R25-10：资料删除确认页预检（PRD V25-GEN-FR-02）——将影响卡片数按可见谓词计数，
    引用任务为静默取消语义（无 blocker，can_delete 恒 true）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    uid = _user_id(db)
    seed = _seed_parsed_project(db, uid, chapters=2, confirmed=True)
    pid, mid = seed["project_id"], seed["file_id"]
    deck = _seed_deck(db, uid, pid)
    _seed_preflight_cards(db, uid, deck, seed["chapter_ids"])
    _seed_task(db, uid, pid, mid, status="GENERATING", chapter_ids=seed["chapter_ids"][:1])
    resp = client.get(f"/projects/{pid}/materials/{mid}/deletion-preflight", headers=user)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resource_type"] == "MATERIAL"
    assert body["resource_id"] == mid
    assert body["can_delete"] is True
    assert body["blockers"] == []  # V25-D-30：静默取消，不向用户暴露任务选项
    impact = body["impact"]
    assert impact["affected_card_count"] == 2  # STAGED 与删除批次中的卡不可见，不计入
    assert impact["chapter_count"] == 2
    assert impact["silently_cancelled_task_count"] == 1
    # 跨用户与他项目资料 → 统一 404（不暴露存在性）
    other = _user(client, username="bob", password="secret-pass-2")
    assert (
        client.get(f"/projects/{pid}/materials/{mid}/deletion-preflight", headers=other).status_code
        == 404
    )
    assert (
        client.get(
            f"/projects/{pid}/materials/{uuid.uuid4()}/deletion-preflight", headers=user
        ).status_code
        == 404
    )


def test_projects_create_requires_idempotency_key(client: TestClient) -> None:
    """写接口强制 Idempotency-Key（契约 1.3）。"""
    user = _user(client)
    resp = client.post(
        "/projects",
        json={"name": "任意"},
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
    created = _create_project(client, user_a, "隔离项目")
    items_a = client.get("/projects", headers=user_a).json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["name"] == "隔离项目"
    assert items_a[0]["project_id"] == created["project_id"]
    assert client.get("/projects", headers=user_b).json() == {"items": []}


def test_projects_list_includes_legacy_project_without_materials(
    client: TestClient, tmp_path: Path
) -> None:
    """V25-D-29 资料集合语义：无资料的历史项目不再被跳过，按空项目（EMPTY）列出。"""
    user = _user(client)
    db_path = tmp_path / "projects_api.db"
    with sqlite3.connect(db_path) as connection:
        # Raw legacy SQLite data may predate FK/NOT NULL enforcement.
        connection.execute(
            "INSERT INTO learning_projects "
            "(project_id, user_id, name, chapters_confirmed_at, version, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                _user_id(db_path),
                "legacy shell",
                "2026-08-28T00:00:00.000Z",
                "2026-08-28T00:00:00.000Z",
                "2026-08-28T00:00:00.000Z",
            ),
        )

    response = client.get("/projects", headers=user)

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "legacy shell"
    assert items[0]["status"] == "EMPTY"
    assert items[0]["materials"] == []


def test_projects_list_ordered_by_updated_desc(client: TestClient) -> None:
    """列表按 updated_at 倒序（最近使用在前）；重命名刷新排序。"""
    user = _user(client)
    first = _create_project(client, user, "第一本")
    second = _create_project(client, user, "第二本")
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
    """详情派生计数（chapter/deck/task）+ PARSED 时 chapters 摘要；多任务每项目。"""
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
    assert [c["chapter_id"] for c in body["chapters"]] == project["chapter_ids"]
    assert body["materials"][0]["material_id"] == project["file_id"]
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
        json={"name": "新名称"},
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


def test_projects_confirm_chapters_before_ready_409(client: TestClient) -> None:
    """无已就绪资料（EMPTY 项目）→ 确认章节 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    project_id = _create_project(client, user, "空项目")["project_id"]
    resp = client.post(
        f"/projects/{project_id}/confirm-chapters",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"


def test_projects_confirm_chapters_parsing_pdf_409(client: TestClient) -> None:
    """PDF 解析中（无已确认章节）→ 确认章节 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    project_id = _create_project(client, user)["project_id"]
    _add_pdf_material(client, user, project_id)
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
    """仅 FAILED 的 PDF 资料可替换：PARSED → 409；FAILED → 200 原子替换重新解析。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    # 1) PARSED 资料替换 → 409 PROJECT_STATE_CONFLICT
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.post(
        f"/projects/{project['project_id']}/materials/{project['file_id']}/replace",
        files={"file": ("new.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"
    # 2) 解析失败资料（HTTP 上传坏 PDF + 扫描失败）→ 可替换
    resp = client.post("/projects", json={"name": "bad"}, headers={**user, **_idem()})
    assert resp.status_code == 201
    project_id = resp.json()["project_id"]
    material = _add_pdf_material(client, user, project_id, "bad.pdf")
    old_material_id = material["material_id"]
    app = cast(Any, client.app)
    scan_once(app.state.session_factory, storage=app.state.storage)
    body = client.get(f"/projects/{project_id}", headers=user).json()
    assert body["status"] == "PARSE_FAILED"
    resp = client.post(
        f"/projects/{project_id}/materials/{old_material_id}/replace",
        files={"file": ("good.pdf", _pdf_bytes(), "application/pdf")},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "bad"  # 项目名保留
    assert body["materials"][0]["material_id"] != old_material_id  # 原子替换为新资料
    assert body["materials"][0]["name"] == "good.pdf"
    assert body["materials"][0]["status"] in ("PENDING", "PARSING")  # 重新解析
    assert body["status"] == "PARSING"
    # 旧资料不在资料列表中
    materials = client.get(f"/projects/{project_id}/materials", headers=user).json()["items"]
    assert [m["material_id"] for m in materials] != old_material_id


def test_projects_replace_pdf_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.post(
        f"/projects/{project['project_id']}/materials/{project['file_id']}/replace",
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


def test_projects_chapter_patch_not_parsed_409(client: TestClient, tmp_path: Path) -> None:
    """资料未解析完成（PENDING）时改章节 → 409 PROJECT_STATE_CONFLICT
    （V25-D-29 多资料：未知章节先行 404，状态栅栏按真实章节触发）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    from sqlalchemy import text as _sqltext

    from infra.db.session import create_db_engine as _engine_for

    engine = _engine_for(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            _sqltext("UPDATE pdf_files SET status = 'PENDING' WHERE file_id = :f"),
            {"f": str(project["file_id"])},
        )
    engine.dispose()
    resp = client.patch(
        f"/projects/{project['project_id']}/chapters/{project['chapter_ids'][0]}",
        json={"name": "x"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"


def test_projects_chapter_patch_unknown_404(client: TestClient, tmp_path: Path) -> None:
    """未知章节改/删 → 404 CHAPTER_NOT_FOUND（不暴露项目存在性之外的信息）。"""
    user = _user(client)
    db = tmp_path / "projects_api.db"
    project = _seed_parsed_project(db, _user_id(db))
    resp = client.patch(
        f"/projects/{project['project_id']}/chapters/{uuid.uuid4()}",
        json={"name": "x"},
        headers={**user, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "CHAPTER_NOT_FOUND"


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
        # 契约 3.17 卡组计划范围与双目标（get-or-create 默认值）
        "selected_deck_ids": [],
        "daily_new_goal": 10,
        "daily_review_goal": 40,
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


def test_projects_delete_parsing_succeeds(client: TestClient) -> None:
    """解析中项目可删（契约 570）：删除自增 parse_version 栅栏迟到解析，不再 409。"""
    user = _user(client)
    project_id = _create_project(client, user)["project_id"]
    _add_pdf_material(client, user, project_id)
    resp = client.delete(f"/projects/{project_id}?retain_decks=true", headers={**user, **_idem()})
    assert resp.status_code == 204
    assert client.get(f"/projects/{project_id}", headers=user).status_code == 404


def test_projects_delete_active_task_auto_cancelled(client: TestClient, tmp_path: Path) -> None:
    """契约 570/675：活跃任务在同一写事务内自动取消，删除不再 409。"""
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
    assert resp.status_code == 204
    assert client.get(f"/projects/{project['project_id']}", headers=user).status_code == 404


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


# ---------- PDF 资料上传三重校验（/pdfs 移除后由 materials/pdf 承接）----------


def test_projects_add_pdf_material_invalid_magic_400(client: TestClient) -> None:
    project_id = _create_project(client, _user(client))["project_id"]
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": ("a.pdf", b"not a pdf", "application/pdf")},
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_projects_add_pdf_material_invalid_extension_400(client: TestClient) -> None:
    project_id = _create_project(client, _user(client))["project_id"]
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": ("a.txt", _pdf_bytes(), "application/pdf")},
        headers={**_user(client, "user2", "pass-2222"), **_idem()},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "PDF_UPLOAD_INVALID"


def test_projects_add_pdf_material_content_length_precheck_400(client: TestClient) -> None:
    """final review I-1：伪造超大 Content-Length 头（110MB > 100MB 上限）+ 合法小 body → 400
    PDF_UPLOAD_INVALID；BodyCaptureMiddleware 在读 body 前按头预检拒绝（不读 body）。"""
    project_id = _create_project(client, _user(client))["project_id"]
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": ("big.pdf", _pdf_bytes(), "application/pdf")},
        headers={**_user(client), **_idem(), "Content-Length": "110000000"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "PDF_UPLOAD_INVALID"
    assert body["error"]["localization_key"] == "error.pdf_upload_invalid"


def test_projects_add_pdf_material_cross_user_404(client: TestClient) -> None:
    """他人项目的资料端点 → 404（统一不暴露存在性）。"""
    _user(client)  # alice 先注册（种子依赖）
    other = _user(client, "user2", "pass-2222")
    project_id = _create_project(client, other)["project_id"]
    resp = client.post(
        f"/projects/{project_id}/materials/pdf",
        files={"file": ("a.pdf", _pdf_bytes(), "application/pdf")},
        headers={**_user(client, "user3", "pass-3333"), **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"
