"""学习项目删除集成测试（V2.5；迁移 schema + HTTP）：两种用户决策（retain_decks）+ 章节删除
两决策（delete_cards）+ 存储失败补偿（回滚元数据，绝不宣称成功却半删）+ 活跃任务/状态保护。

契约锚点：structure-contract 3.16/6.2；PRD V25-GEN-FR-02/09；openapi DELETE /projects。
"""

import json
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import auth_headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "project_del.db"
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
    return auth_headers(client, username=username, password=password)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
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


def _db(db_path: Path) -> tuple[Any, Any]:
    """种子用 session factory（迁移后 schema，ORM 直种）。"""
    from sqlalchemy.orm import Session, sessionmaker

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    return factory, engine


def _seed_project(db_path: Path, user_id: str, *, status: str = "PARSED") -> dict[str, Any]:
    """种子项目（PENDING/PARSED/FAILED）+ 章节；返回 {project_id, file_id, chapter_ids}。"""
    from infra.db.models import Chapter, LearningProject, PdfFile

    factory, engine = _db(db_path)
    project_id, file_id = str(uuid.uuid4()), str(uuid.uuid4())
    chapter_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    with factory() as session:
        session.add(
            PdfFile(
                file_id=file_id,
                user_id=user_id,
                filename="seed.pdf",
                storage_key="a" * 32,
                size_bytes=100,
                status=status,
                error_code="PDF_TOC_MISSING" if status == "FAILED" else None,
                created_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.flush()
        if status == "PARSED":
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
                chapters_confirmed_at="2026-08-15T00:00:00.000Z",
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
    from infra.db.models import Task

    factory, engine = _db(db_path)
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


def _seed_deck_with_card(
    db_path: Path,
    user_id: str,
    project_id: str,
    *,
    chapter_id: str | None = None,
    with_review: bool = False,
    with_event: bool = False,
) -> tuple[str, str]:
    """种子归属项目的牌组 + 卡片（可带 chapter_id / 复习数据）。返回 (deck_id, card_id)。"""
    from infra.db.models import Card, Deck, ReviewEvent, ReviewState

    factory, engine = _db(db_path)
    deck_id, card_id = str(uuid.uuid4()), str(uuid.uuid4())
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
        session.flush()
        session.add(
            Card(
                card_id=card_id,
                deck_id=deck_id,
                user_id=user_id,
                source="GENERATED",
                position=1,
                front="f",
                back="b",
                card_type="QUESTION",
                question="q",
                answer="a",
                chapter_id=chapter_id,
                version="v1",
                created_at="2026-08-15T00:00:00.000Z",
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.flush()
        if with_review:
            session.add(
                ReviewState(
                    review_state_id=str(uuid.uuid4()),
                    card_id=card_id,
                    state="NEW",
                    stability=0.0,
                    difficulty=1.0,
                    due="2026-08-15T00:00:00.000Z",
                    reps=0,
                    lapses=0,
                    updated_at="2026-08-15T00:00:00.000Z",
                )
            )
        if with_event:
            session.add(
                ReviewEvent(
                    review_event_id=str(uuid.uuid4()),
                    user_id=user_id,
                    card_id=card_id,
                    client_event_id=str(uuid.uuid4()),
                    rating="GOOD",
                    reviewed_at="2026-08-15T00:00:00.000Z",
                    created_at="2026-08-15T00:00:00.000Z",
                )
            )
        session.commit()
    engine.dispose()
    return deck_id, card_id


def _seed_kp(db_path: Path, user_id: str, task_id: str, chapter_id: str) -> None:
    """种子知识点（章节删除时 chapter_id 置 null 的关联）。"""
    from infra.db.models import KnowledgePoint

    factory, engine = _db(db_path)
    with factory() as session:
        session.add(
            KnowledgePoint(
                knowledge_point_id=str(uuid.uuid4()),
                task_id=task_id,
                chapter_id=chapter_id,
                source_chunk_id="c1",
                topic="t",
                priority=1,
                status="PENDING",
            )
        )
        session.commit()
    engine.dispose()


def _seed_preferences(db_path: Path, user_id: str, project_id: str) -> None:
    """种子账号偏好 current_project_id → 项目删除时置空。"""
    from infra.db.models import UserPreferences

    factory, engine = _db(db_path)
    with factory() as session:
        session.add(
            UserPreferences(
                user_id=user_id,
                coverage_mode="BALANCED",
                basic_ratio=40,
                understanding_ratio=40,
                deep_question_ratio=20,
                daily_goal=50,
                learning_timezone="Asia/Shanghai",
                current_project_id=project_id,
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.commit()
    engine.dispose()


def _seed_settings(db_path: Path, project_id: str, chapter_ids: list[str]) -> None:
    """种子项目学习设置（章节删除时范围同步移除）。"""
    from infra.db.models import ProjectStudySettings

    factory, engine = _db(db_path)
    with factory() as session:
        session.add(
            ProjectStudySettings(
                project_id=project_id,
                selected_chapter_ids=json.dumps(chapter_ids, ensure_ascii=False),
                include_unassigned=1,
                updated_at="2026-08-15T00:00:00.000Z",
            )
        )
        session.commit()
    engine.dispose()


def _scalar(db_path: Path, sql: str, **params: object) -> object:
    from sqlalchemy import text

    from infra.db.session import create_db_engine

    engine = create_db_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _write_storage(client: TestClient, storage_key: str) -> Path:
    """写存储对象（模拟上传/扫描产物）。"""
    storage = cast(Any, client.app).state.storage
    obj = storage.open(storage_key)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"%PDF-1.4 fake")
    return cast(Path, obj)


def _error_code(resp: Any) -> str:
    return str(resp.json()["error"]["code"])


# ---------- 删除决策一：保留牌组与卡片 ----------


def test_delete_project_retain_decks_true_keeps_decks_cards(
    client: TestClient, tmp_path: Path
) -> None:
    """retain_decks=true：PDF/章节/任务历史/项目配置删除；牌组/卡片/复习数据保留并脱离项目；
    cards.chapter_id 置空（进入未归属章节）；current_project_id 置空。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    deck_id, card_id = _seed_deck_with_card(
        db,
        user_id,
        str(project["project_id"]),
        chapter_id=str(project["chapter_ids"][0]),
        with_review=True,
        with_event=True,
    )
    _seed_task(db, user_id, str(project["project_id"]), str(project["file_id"]))  # 终态任务
    _seed_preferences(db, user_id, str(project["project_id"]))
    obj = _write_storage(client, "a" * 32)

    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=true", headers={**user, **_idem()}
    )
    assert resp.status_code == 204, resp.text

    # 项目/PDF/章节/任务历史/设置全删
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM pdf_files") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM chapters") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM tasks") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM project_study_settings") == 0
    assert not obj.exists()  # 存储对象随删除清理
    # 牌组/卡片/复习数据保留；牌组脱离项目；卡片 chapter_id 置空；偏好 current_project 置空
    assert _scalar(db, "SELECT project_id FROM decks WHERE deck_id = :d", d=deck_id) is None
    assert _scalar(db, "SELECT COUNT(*) FROM cards WHERE card_id = :c", c=card_id) == 1
    assert _scalar(db, "SELECT chapter_id FROM cards WHERE card_id = :c", c=card_id) is None
    assert _scalar(db, "SELECT COUNT(*) FROM review_states") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM review_events") == 1
    assert _scalar(db, "SELECT current_project_id FROM user_preferences") is None


# ---------- 删除决策二：删除整个聚合 ----------


def test_delete_project_retain_decks_false_removes_everything(
    client: TestClient, tmp_path: Path
) -> None:
    """retain_decks=false：牌组/卡片/复习数据（含学习记录）一并删除。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    _seed_deck_with_card(db, user_id, str(project["project_id"]), with_review=True, with_event=True)
    _seed_task(db, user_id, str(project["project_id"]), str(project["file_id"]))
    _seed_preferences(db, user_id, str(project["project_id"]))

    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=false", headers={**user, **_idem()}
    )
    assert resp.status_code == 204, resp.text

    for table in (
        "learning_projects",
        "pdf_files",
        "chapters",
        "tasks",
        "project_study_settings",
        "decks",
        "cards",
        "review_states",
        "review_events",
    ):
        assert _scalar(db, f"SELECT COUNT(*) FROM {table}") == 0, table
    assert _scalar(db, "SELECT current_project_id FROM user_preferences") is None


# ---------- 删除保护 ----------


def test_delete_project_active_task_conflict_nothing_deleted(
    client: TestClient, tmp_path: Path
) -> None:
    """活跃任务阻止删除：409 PROJECT_HAS_ACTIVE_TASK，无任何数据被删。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    _seed_task(
        db,
        user_id,
        str(project["project_id"]),
        str(project["file_id"]),
        status="SAMPLE_GENERATING",
    )
    _seed_deck_with_card(db, user_id, str(project["project_id"]))

    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=false", headers={**user, **_idem()}
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_HAS_ACTIVE_TASK"
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM decks") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM tasks") == 1


def test_delete_project_parsing_conflict(client: TestClient, tmp_path: Path) -> None:
    """解析中（PENDING）项目不可删除 → 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    project = _seed_project(db, _user_id(db), status="PENDING")
    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=true", headers={**user, **_idem()}
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 1


def test_delete_project_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "project_del.db"
    project = _seed_project(db, _user_id(db))
    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=true", headers={**user_b, **_idem()}
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 1


# ---------- 存储失败补偿 ----------


def test_delete_project_storage_failure_rolls_back_metadata(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存储清理失败 → 500 且元数据整体回滚（绝不宣称成功却半删）；修复后可重试成功。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    _seed_task(db, user_id, str(project["project_id"]), str(project["file_id"]))
    _seed_deck_with_card(db, user_id, str(project["project_id"]))
    obj = _write_storage(client, "a" * 32)

    storage = cast(Any, client.app).state.storage
    monkeypatch.setattr(storage, "delete", lambda key: (_ for _ in ()).throw(OSError("disk full")))
    # 500 断言须关 raise_server_exceptions（Starlette 对未处理异常先发 500 再重抛，
    # 默认 TestClient 会把 OSError 重抛给测试）——test_request_logging 同款
    failing_client = TestClient(client.app, raise_server_exceptions=False)
    user_f = _user(failing_client)  # 同库已注册 → login 复用

    resp = failing_client.delete(
        f"/projects/{project['project_id']}?retain_decks=false", headers={**user_f, **_idem()}
    )
    assert resp.status_code == 500
    assert _error_code(resp) == "INTERNAL_ERROR"
    # 元数据完整回滚：项目/任务/牌组/PDF 均未删；存储对象仍在（可重试）
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM tasks") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM decks") == 1
    assert _scalar(db, "SELECT COUNT(*) FROM pdf_files") == 1
    assert obj.exists()

    # 恢复存储后重试 → 204，全部删除
    monkeypatch.undo()
    resp = client.delete(
        f"/projects/{project['project_id']}?retain_decks=false", headers={**user, **_idem()}
    )
    assert resp.status_code == 204, resp.text
    assert _scalar(db, "SELECT COUNT(*) FROM learning_projects") == 0
    assert not obj.exists()


# ---------- 章节删除（delete_cards 两决策 + 保护） ----------


def test_delete_chapter_default_keeps_cards_unassigned(client: TestClient, tmp_path: Path) -> None:
    """delete_cards=false（默认）：卡保留、chapter_id 置空（进入未归属章节）；KP chapter_id 置空；
    章节从新卡范围移除。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    chapter_id = str(project["chapter_ids"][0])
    _deck, card_id = _seed_deck_with_card(
        db, user_id, str(project["project_id"]), chapter_id=chapter_id
    )
    task_id = _seed_task(
        db,
        user_id,
        str(project["project_id"]),
        str(project["file_id"]),
        status="FAILED",
        chapter_ids=[chapter_id],
    )
    _seed_kp(db, user_id, task_id, chapter_id)
    _seed_settings(db, str(project["project_id"]), project["chapter_ids"])

    resp = client.delete(
        f"/projects/{project['project_id']}/chapters/{chapter_id}",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 204, resp.text
    assert _scalar(db, "SELECT COUNT(*) FROM chapters") == 1  # 另一章仍在
    assert _scalar(db, "SELECT COUNT(*) FROM cards WHERE card_id = :c", c=card_id) == 1
    assert _scalar(db, "SELECT chapter_id FROM cards WHERE card_id = :c", c=card_id) is None
    assert _scalar(db, "SELECT chapter_id FROM knowledge_points") is None
    remaining = _scalar(db, "SELECT selected_chapter_ids FROM project_study_settings")
    assert remaining is not None
    assert json.loads(str(remaining)) == [project["chapter_ids"][1]]


def test_delete_chapter_with_delete_cards_removes_cards_and_review(
    client: TestClient, tmp_path: Path
) -> None:
    """delete_cards=true：该章节生成的卡片与复习数据一并删除；KP chapter_id 置空。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    chapter_id = str(project["chapter_ids"][0])
    _deck, card_id = _seed_deck_with_card(
        db,
        user_id,
        str(project["project_id"]),
        chapter_id=chapter_id,
        with_review=True,
        with_event=True,
    )
    # 另一章节的卡不受影响
    _deck2, other_card = _seed_deck_with_card(
        db, user_id, str(project["project_id"]), chapter_id=str(project["chapter_ids"][1])
    )
    resp = client.delete(
        f"/projects/{project['project_id']}/chapters/{chapter_id}?delete_cards=true",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 204, resp.text
    assert _scalar(db, "SELECT COUNT(*) FROM cards WHERE card_id = :c", c=card_id) == 0
    assert _scalar(db, "SELECT COUNT(*) FROM cards WHERE card_id = :c", c=other_card) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM review_states") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM review_events") == 0
    assert _scalar(db, "SELECT COUNT(*) FROM chapters") == 1


def test_delete_chapter_active_task_conflict(client: TestClient, tmp_path: Path) -> None:
    """被活跃任务引用的章节不可删除 → 409 PROJECT_HAS_ACTIVE_TASK。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    user_id = _user_id(db)
    project = _seed_project(db, user_id)
    chapter_id = str(project["chapter_ids"][0])
    _seed_task(
        db,
        user_id,
        str(project["project_id"]),
        str(project["file_id"]),
        status="AWAITING_SAMPLE_CONFIRMATION",
        chapter_ids=[chapter_id],
    )
    resp = client.delete(
        f"/projects/{project['project_id']}/chapters/{chapter_id}",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_HAS_ACTIVE_TASK"
    assert _scalar(db, "SELECT COUNT(*) FROM chapters") == 2


def test_delete_chapter_cross_user_404(client: TestClient, tmp_path: Path) -> None:
    _user(client)  # alice 先注册（种子依赖）
    user_b = _user(client, "user2", "pass-2222")
    db = tmp_path / "project_del.db"
    project = _seed_project(db, _user_id(db))
    resp = client.delete(
        f"/projects/{project['project_id']}/chapters/{project['chapter_ids'][0]}",
        headers={**user_b, **_idem()},
    )
    assert resp.status_code == 404
    assert _error_code(resp) == "PROJECT_NOT_FOUND"


def test_delete_chapter_not_parsed_409(client: TestClient, tmp_path: Path) -> None:
    """未解析项目删章节 → 409 PROJECT_STATE_CONFLICT。"""
    user = _user(client)
    db = tmp_path / "project_del.db"
    project = _seed_project(db, _user_id(db), status="PENDING")
    resp = client.delete(
        f"/projects/{project['project_id']}/chapters/{uuid.uuid4()}",
        headers={**user, **_idem()},
    )
    assert resp.status_code == 409
    assert _error_code(resp) == "PROJECT_STATE_CONFLICT"
