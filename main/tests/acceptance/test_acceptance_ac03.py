"""验收测试：AC-03 样卡（PRD 5.6/AC-03；迁移 schema + HTTP + 种 DB 行）。

V2.5 语义（structure-contract 3.4/4.3）：样卡持久化于任务——POST /projects/{id}/tasks
建 DRAFT → POST /tasks/{id}/samples 请求 → 样卡 worker 后台完成（fake 确定性生成）→
GET 任务读取 sample_cards。旧 /samples 兼容端点已随 Task 5 移除。

映射（PRD AC-03 四条）：
- AC-03-a 每次生成固定输出 3 张样卡 → len(sample_cards) == 3
- AC-03-b 三档难度各有 1 张 → target_difficulty == {BASIC, UNDERSTANDING, DEEP_QUESTION}（V2.5 改名）
- AC-03-c DEEP_QUESTION 只允许 QUESTION 卡型 → card_type 计 QUESTION==3 / TRUE_FALSE==0
- AC-03-d 不能编辑且不入牌组不统计 → 样卡只存任务列（cards/review_states 无行）：
  "不能编辑"由结构保证（样卡非 Card 行）；正式统计只读库内卡片，无卡片自然不统计。

PDF 上下文（plan Task 5 决策）：种 DB 行（User + PdfFile PARSED + Chapters + 项目 +
牌组 + ApiKey），不依赖样书——AC-03 聚焦样卡构成，PDF 全链路由 AC-01 覆盖。
"""

import json
import uuid
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.config import Settings
from app.main import create_app
from infra.db.models import (
    ApiKey,
    Card,
    Chapter,
    Deck,
    LearningProject,
    PdfFile,
    ReviewState,
    Task,
    TextChunk,
    User,
)
from infra.llm.crypto import encrypt_key, key_from_settings
from services.tasks.executor import process_active_tasks
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]

_NOW = "2026-08-11T00:00:00.000Z"


def _test_settings() -> Settings:
    # _env_file=None：测试确定性——不加载仓库根 .env（真实 Key 不进测试进程）
    return Settings(api_key_encryption_key="aa" * 32, _env_file=None)  # type: ignore[call-arg]


def _envelope(user_prompt: str) -> dict[str, object]:
    raw = user_prompt.split("<GENERATOR_INPUT>")[1].split("</GENERATOR_INPUT>")[0]
    return cast("dict[str, object]", json.loads(raw))


class StubClient:
    """样卡 worker 注入的假 LLM：按信封 target_difficulty 返回合规 QUESTION 卡（不触网）。"""

    def close(self) -> None:
        pass

    def chat(
        self,
        prompt: str,
        api_key: str = "",
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        payload = _envelope(prompt)
        difficulty = str(payload["target_difficulty"])
        return {
            "content": json.dumps(
                {
                    "cards": [
                        {
                            "type": "QUESTION",
                            "question": f"样卡问题-{difficulty}",
                            "answer": f"样卡答案-{difficulty}",
                        }
                    ]
                }
            ),
            "usage": {"prompt_cache_miss_tokens": 5, "completion_tokens": 3},
            "model": "deepseek-v4-flash",
            "http_status": 200,
            "duration_ms": 1,
        }


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
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式驱动样卡 worker
    )
    return TestClient(create_app(settings))


def _user(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _config() -> dict[str, object]:
    return {
        "coverage_mode": "BALANCED",
        "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
    }


def _seed_pdf_context(client: TestClient, user: dict[str, str]) -> dict[str, object]:
    """种 DB 行：User（FK 前置）+ PdfFile(PARSED) + 项目 + 牌组（绑定项目）+ 2 章节
    + ApiKey（create_task 校验 Key）。"""
    app = cast(FastAPI, client.app)
    factory = app.state.session_factory
    with factory() as session:
        owner = session.scalar(select(User).where(User.username == "alice"))  # 注册端点已建行
        assert owner is not None
        pdf = PdfFile(
            file_id=str(uuid.uuid4()),
            user_id=owner.user_id,
            filename="b.pdf",
            storage_key=str(uuid.uuid4()),
            size_bytes=10,
            status="PARSED",
            created_at=_NOW,
        )
        session.add(pdf)
        session.flush()
        project = LearningProject(
            project_id=str(uuid.uuid4()),
            user_id=owner.user_id,
            file_id=pdf.file_id,
            name="P",
            chapters_confirmed_at=_NOW,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(project)
        session.flush()
        deck = Deck(
            deck_id=str(uuid.uuid4()),
            user_id=owner.user_id,
            name="D",
            source="MANUAL",
            project_id=project.project_id,
            version=_NOW,
            created_at=_NOW,
            updated_at=_NOW,
        )
        session.add(deck)
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
        encryption_key = key_from_settings(_test_settings())
        assert encryption_key is not None
        session.execute(
            insert(ApiKey).values(
                user_id=owner.user_id,
                encrypted_key=encrypt_key("sk-test-0123456789", encryption_key),
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at=_NOW,
            )
        )
        # 样卡真实生成需章节文本：首章 1-2 页、次章 2-3 页 → 种页 1..3
        for page in range(1, 4):
            session.execute(
                insert(TextChunk).values(
                    chunk_id=str(uuid.uuid4()),
                    file_id=pdf.file_id,
                    page_number=page,
                    char_count=20,
                    content_sha256="0" * 64,
                    content=f"第 {page} 页：上下文工程核心概念。",
                    created_at=_NOW,
                )
            )
        session.commit()
    return {"project_id": project.project_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_acceptance_ac03_sample_cards(client: TestClient) -> None:
    """AC-03：3 张样卡（三档难度各 1；2 问答 + 1 判断）；不入牌组不统计。"""
    user = _user(client)
    seed = _seed_pdf_context(client, user)
    resp = client.post(
        f"/projects/{seed['project_id']}/tasks",
        json={
            "deck_id": seed["deck_id"],
            "chapter_ids": seed["chapter_ids"],
            "generation_config": _config(),
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    # 请求样卡（DRAFT → SAMPLE_GENERATING）→ 样卡 worker 后台完成 → AWAITING
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    app = cast(FastAPI, client.app)
    with app.state.session_factory() as session:
        process_active_tasks(
            session, settings=_test_settings(), client_factory=lambda _key: StubClient()
        )
        session.commit()
    resp = client.get(f"/tasks/{task_id}", headers=user)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "AWAITING_SAMPLE_CONFIRMATION"
    cards = body["sample_cards"]
    # AC-03-a：固定 3 张（比例>0 的难度各 1）
    assert len(cards) == 3
    # AC-03-b：三档难度各有 1 张
    assert {c["target_difficulty"] for c in cards} == {
        "BASIC",
        "UNDERSTANDING",
        "DEEP_QUESTION",
    }  # V2.5 改名
    # AC-03-c：DEEP_QUESTION 只允许 QUESTION 卡型（V2.5 契约 3.6）
    assert sum(1 for c in cards if c["card_type"] == "QUESTION") == 3
    assert sum(1 for c in cards if c["card_type"] == "TRUE_FALSE") == 0
    # R-14：SampleCard 轻量组件（structure-contract 3.13）——无落库/归属/版本占位字段
    for c in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(c)
        assert {"deck_id", "position", "created_at", "updated_at"} & set(c) == set()
    # AC-03-d：不入牌组不统计——样卡只存任务列（cards/review_states 无行），统计自然不含样卡
    app = cast(FastAPI, client.app)
    with app.state.session_factory() as session:
        assert session.scalar(select(Card).limit(1)) is None
        assert session.scalar(select(ReviewState).limit(1)) is None
        assert session.get(Task, task_id) is not None  # 样卡持久化于任务
