"""任务生成 E2E 判别测试（P4-5 跟进④）：API 全链路建任务端到端生成成功（user 域无断裂）。

链路：register（Bearer）→ PUT /api-key（mock transport 假 Key，加密落库）→ 建学习项目
（上传样书 PDF，HTTP 201）→ pdf scanner 显式扫描（PARSED + 章节 + 页文本）→ POST /decks
（HTTP，归属项目）→ POST /projects/{project_id}/tasks（HTTP 201，Key 校验按 user_id）
→ tasks executor 显式扫描（mock transport 全管线）→ 任务 COMPLETED 且卡片 user_id 非空
（归属切 user 域判别）。

与 test_tasks_api.py 的差异：本文件零种子直写——PDF/章节/页文本/牌组/Key 全部经 HTTP
与真实解析链落地；T4 已修 executor Key 查找切 user 域，本链路若已绿则作为回归守卫。
"""

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.deepseek import DeepSeekClient
from services.pdf.scanner import scan_once as scan_pdfs
from services.tasks.executor import scan_once as scan_tasks
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/integration/ → 仓库根
SAMPLE = REPO_ROOT / "res" / "AI-Agents-in-Depth-zh-CN.pdf"

# executor 解密路径与 PUT /api-key 落库共用同一测试加密密钥（hex 32B）
_SETTINGS = Settings(api_key_encryption_key="aa" * 32)


class FakeClient:
    """mock transport（PUT /api-key）：validate_key 恒 AVAILABLE；close() no-op（不触网）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        pass

    def validate_key(self, api_key: str) -> str:
        return "AVAILABLE"


def _client_factory(api_key: str) -> DeepSeekClient:
    """mock transport 全链路分派（与 test_tasks_api 同款）：<PLANNER_INPUT> → 按请求配额
    产出锚定单元；<SCORING_INPUT> → ID 守恒的确定性分数；其余（<GENERATOR_INPUT>）→
    每批 1 张合法卡。COMPACT 2 章 = 6 单元 → 6 批 → 6 卡。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        if "<PLANNER_INPUT>" in user:
            payload = json.loads(
                user.split("<PLANNER_INPUT>", 1)[1].split("</PLANNER_INPUT>", 1)[0]
            )
            chunk_ids = [c["chunk_id"] for c in payload["source_chunks"]]
            units: list[dict[str, object]] = []
            for difficulty, quota in payload["difficulty_quota"].items():
                for _ in range(quota):
                    units.append(
                        {
                            "source_chunk_ids": [chunk_ids[0]],
                            "learning_objective": f"知识点{len(units)}",
                            "target_difficulty": difficulty,
                            "card_type": "QUESTION",
                            "coverage_tier": "CORE",  # V2.5 资产 v3：语义单元 tier 必填
                        }
                    )
            content = json.dumps({"units": units}, ensure_ascii=False)
        elif "<SCORING_INPUT>" in user:
            payload = json.loads(
                user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0]
            )
            content = json.dumps(
                {
                    "scores": [
                        {
                            "generation_item_id": item["generation_item_id"],
                            "evidence_score": 2,
                            "correctness_score": 3,
                            "difficulty_score": 2,
                            "learning_value_score": 2,
                        }
                        for item in payload["items"]
                    ]
                },
                ensure_ascii=False,
            )
        else:  # 生成调用：1 单元 1 批 → 每批 1 张合法卡
            content = json.dumps(
                {"cards": [{"type": "QUESTION", "question": "q0", "answer": "a0"}]},
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "deepseek-v4-flash",
            },
        )

    return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))


@pytest.fixture
def ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "task_e2e.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶，显式调高隔离
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
        api_key_encryption_key="aa" * 32,  # PUT 落库加密与 executor 解密共用测试密钥
    )
    monkeypatch.setattr("app.api.api_key.DeepSeekClient", FakeClient)
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _scan_pdfs(client: TestClient) -> None:
    """显式触发 PDF 解析扫描（测试环境无后台循环）：从 app state 取 session_factory/storage。"""
    app = cast(FastAPI, client.app)
    scan_pdfs(app.state.session_factory, storage=app.state.storage)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def test_task_e2e_user_domain_generation(ctx: tuple[TestClient, Path]) -> None:
    """P4-5 跟进④：register → PUT /api-key → 上传样书 → 解析 → 建牌组 → 建任务 →
    executor 扫描 → COMPLETED 且卡片 user_id 非空（若已绿为回归守卫，T4 已修 Key 查找）。"""
    client, db_path = ctx
    if not SAMPLE.exists():
        pytest.skip("样书缺失")
    user = auth_headers(client)

    # 1. PUT /api-key（mock transport 假 Key；加密落库 user 域行）
    resp = client.put("/api-key", json={"api_key": "sk-test-e2e-abcd"}, headers={**user, **_idem()})
    assert resp.status_code == 200
    assert resp.json()["status"] == "AVAILABLE"

    # 2. 建学习项目（上传样书 → 201）；pdf scanner 显式扫描 → PARSED + 章节
    with SAMPLE.open("rb") as f:
        resp = client.post(
            "/projects",
            files={"file": ("book.pdf", f, "application/pdf")},
            headers={**user, **_idem()},
        )
    assert resp.status_code == 201
    project_id = resp.json()["project_id"]
    _scan_pdfs(client)
    project = client.get(f"/projects/{project_id}", headers=user).json()
    assert project["file"]["status"] == "PARSED"
    chapters = project["file"]["chapters"]
    assert len(chapters) >= 3

    # 3. 建牌组（HTTP，归属项目——V2.5 6.4 同项目校验）
    deck_resp = client.post(
        "/decks", json={"name": "D", "project_id": project_id}, headers={**user, **_idem()}
    )
    assert deck_resp.status_code == 201
    deck_id = deck_resp.json()["deck_id"]

    # 4. 建任务（前 2 章 COMPACT；V2.5 项目归属入口；Key 校验按 user_id）
    resp = client.post(
        f"/projects/{project_id}/tasks",
        json={
            "deck_id": deck_id,
            "chapter_ids": [c["chapter_id"] for c in chapters[:2]],
            "generation_config": {
                "coverage_mode": "COMPACT",
                "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
            },
        },
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # 4b. 样卡阶段（V2.5 确认式启动）：触发持久化生成（DRAFT → SAMPLE_GENERATING）→
    #     样卡 worker 扫描 → AWAITING_SAMPLE_CONFIRMATION → start 确认进入 GENERATING
    task_factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    resp = client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()})
    assert resp.status_code == 200
    scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
    resp = client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()})
    assert resp.status_code == 200

    # 5. executor 显式扫描 → COMPLETED（COMPACT 2 章 = 6 单元 → 6 卡）
    final: dict[str, object] = {}
    for _ in range(10):
        scan_tasks(task_factory, settings=_SETTINGS, client_factory=_client_factory)
        resp = client.get(f"/tasks/{task_id}", headers=user)
        assert resp.status_code == 200
        final = resp.json()
        if final["status"] == "COMPLETED":
            break
    assert final["status"] == "COMPLETED"
    assert final["generated_card_count"] == 6
    assert final["ended_at"] is not None

    # 6. 归属判别：全部卡片 user_id 非空（归属切 user 域）
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        cards = conn.execute(text("SELECT card_id, user_id, deck_id FROM cards")).all()
    assert len(cards) == 6
    assert all(row[1] is not None for row in cards), "卡片 user_id 应非空（user 域归属）"
    assert {row[2] for row in cards} == {deck_id}
