"""验收测试：AC-04 正式生成与入库 + AC-07 质量与缓存数据（PRD 9；迁移 schema + HTTP + mock transport）。

LLM 升级管线（规划 → 生成 → 评分）mock 全链路驱动（planner 按请求配额产出锚定单元 →
批=单元 → 每批 1 卡 → 评分回写 Card 5 字段）。AC 验收意图在新语义下的映射：

AC-04-a 按知识点分批生成正式卡片 → POST /tasks → executor 扫描 → 任务 COMPLETED
        + 合法卡入库（Schema 通过）；批次语义换载体：旧"batch_size 每批 3 卡" →
        新"1 单元 1 批、每批 1 卡"（6 单元 → 6 批 → 6 卡）
AC-04-b 只有通过 Schema 校验的卡片入库 → 非法卡（缺 question/answer）→ 批次重试
        预算耗尽 SKIPPED，cards 无行（批次级失败不中断任务）
AC-04-c Rubric 评分不影响 Schema 合法卡入库 → 低分（评分 mock 总分 ≤ 6）但 Schema
        合法的卡仍全部入库（原"低分批次 SUCCEEDED"意图不变，评分来自 SCORING 阶段
        而非生成响应）
AC-04-d 不因 Rubric 执行自动修复/淘汰/补生成 → 低分批次 SUCCEEDED 不重试
        （retry_count=0）、批次数不增加（total==completed）、低分卡不淘汰（仍在牌组）
AC-07-a 单卡 Rubric 评分 + 整批质量记录 → GET /tasks/{id}/batches 含
        rubric_version/质量分布；卡片 5 个 Rubric 分数字段非 null（SCORING 回写）
AC-07-b Prompt Cache 命中/未命中/输出 Token 记录 → batches items 含
        cache_hit/miss + output tokens（生成调用 usage 投影，批=单元 1 卡）
AC-07-c Rubric/Cache 异常不影响入库规则 → usage 缺失（token 观测 None）仍正常
        入库 SUCCEEDED

后台循环间隔拉大（3600s）隔离：测试显式调 executor.scan_once（test_tasks_api 同款"显式
scan_once"模式）；种子直写迁移后 DB（users 前置 + 真实加密 Key——executor 解密路径 +
text_chunks 页文本——规划输入）。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Batch, Card, Chapter, LearningProject, PdfFile, User
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import scan_once as scan_tasks
from tests.conftest import auth_headers

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)

# 评分四维缺省（正常）：总分 = 代码计算 2+3+2+2 = 9（AC-07-a 区间 0 < total ≤ 12）
_DEFAULT_SCORES = {
    "evidence_score": 2,
    "correctness_score": 3,
    "difficulty_score": 2,
    "learning_value_score": 2,
}
# 低分四维（AC-04-c）：总分 = 1+1+0+2 = 4 ≤ 6（Schema 合法但 Rubric 极低）
_LOW_SCORES = {
    "evidence_score": 1,
    "correctness_score": 1,
    "difficulty_score": 0,
    "learning_value_score": 2,
}


def _pipeline_factory(
    *,
    cards: list[dict[str, object]],
    scores: dict[str, int] | None = None,
    with_usage: bool = True,
) -> Callable[[str], DeepSeekClient]:
    """mock transport 全链路分派（planner → generator → scorer）。

    - <PLANNER_INPUT>：按请求配额产出锚定单元（引用请求内组页）；学习目标全局唯一
      编号（跨规划调用共享计数器——生成卡内容可按目标序号定位）。
    - <GENERATOR_INPUT>：从学习目标提取序号 → 每批 1 张卡（cards 按序号循环）。
    - <SCORING_INPUT>：ID 集合守恒的分数（scores 四维；缺省正常分数）。
    """
    counter = [0]
    score = scores or _DEFAULT_SCORES

    def factory(api_key: str) -> DeepSeekClient:
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
                                "learning_objective": f"知识点{counter[0]}",
                                "target_difficulty": difficulty,
                                "card_type": "QUESTION",
                                "coverage_tier": "CORE",
                            }
                        )
                        counter[0] += 1
                content = json.dumps({"units": units}, ensure_ascii=False)
            elif "<SCORING_INPUT>" in user:
                payload = json.loads(
                    user.split("<SCORING_INPUT>", 1)[1].split("</SCORING_INPUT>", 1)[0]
                )
                content = json.dumps(
                    {
                        "scores": [
                            {**score, "generation_item_id": item["generation_item_id"]}
                            for item in payload["items"]
                        ]
                    },
                    ensure_ascii=False,
                )
            else:  # 生成调用：1 单元 1 批 → 每批 1 张卡（按目标序号循环）
                payload = json.loads(
                    user.split("<GENERATOR_INPUT>", 1)[1].split("</GENERATOR_INPUT>", 1)[0]
                )
                index = int(payload["learning_objective"].split("知识点", 1)[1])
                content = json.dumps({"cards": [cards[index % len(cards)]]}, ensure_ascii=False)
            resp_body: dict[str, object] = {
                "choices": [{"message": {"content": content}}],
                "model": "deepseek-v4-flash",
            }
            if with_usage:
                resp_body["usage"] = {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_cache_hit_tokens": 2,
                    "prompt_cache_miss_tokens": 8,
                }
            return httpx.Response(200, json=resp_body)

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    return factory


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac04_ac07.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=100,  # IP 维度隔离：Bearer 注册请求计入 IP 桶（连发 >5 req/s），显式调高隔离,
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path


def _uuid() -> str:
    return str(uuid.uuid4())


def _user(client: TestClient) -> dict[str, str]:
    """已注册用户的 Bearer 头（P4-4 起 X-Device-ID 退出，仅 Bearer）。"""
    return auth_headers(client)


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _user_id(db_path: Path, username: str = "alice") -> str:
    """注册用户（alice）的 user_id（users 表按 username 查询）。"""
    engine = create_db_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT user_id FROM users WHERE username = :u"), {"u": username}
        ).scalar()
    assert row is not None
    return str(row)


def _seed_context(db_path: Path, *, user_id: str) -> dict[str, object]:
    """users 前置 + PDF(PARSED) + 2 章节 + 牌组 + 真实加密 Key（executor 解密路径）
    + 页文本（text_chunks——LLM 升级管线规划输入）。PDF/牌组/Key 均 user 域（P4-4 起——
    ApiKey 用户域 Core 直写（只写所需列）。
    """
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        if session.get(User, user_id) is None:  # 注册端点已建行时复用
            session.add(
                User(
                    user_id=user_id,
                    username=f"u-{user_id[:8]}",
                    email=f"u-{user_id[:8]}@example.com",
                    password_hash="x",
                    created_at="2026-08-11T00:00:00.000Z",
                    updated_at="2026-08-11T00:00:00.000Z",
                )
            )
            session.flush()  # UoW 不按 FK 排序 INSERT（无 relationship）
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
        project = LearningProject(
            project_id=_uuid(),
            user_id=user_id,
            file_id=pdf.file_id,
            name="P",
            chapters_confirmed_at="2026-08-11T00:00:00.000Z",
            version="2026-08-11T00:00:00.000Z",
            created_at="2026-08-11T00:00:00.000Z",
            updated_at="2026-08-11T00:00:00.000Z",
        )
        session.add(project)
        session.flush()
        deck = create_deck(session, user_id=user_id, name="D", now="2026-08-11T00:00:00.000Z")
        deck.project_id = project.project_id  # V2.5：牌组归属项目（6.4 同项目校验）
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
        session.execute(
            insert(ApiKey).values(
                user_id=user_id,
                encrypted_key=_ENCRYPTED_TEST_KEY,
                status="AVAILABLE",
                masked_key="sk-****",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.flush()
        # 章节 1（页 1-2）/ 章节 2（页 2-3）→ 页文本覆盖 1-3
        persist_text_chunks(
            session,
            file_id=pdf.file_id,
            pages=[{"page_number": pn, "content": f"第{pn}页内容" * 20} for pn in (1, 2, 3)],
            now="2026-08-11T00:00:00.000Z",
        )
        session.commit()
    return {
        "project_id": project.project_id,
        "file_id": pdf.file_id,
        "deck_id": deck.deck_id,
        "chapter_ids": chapter_ids,
    }


def _payload(seed: dict[str, object]) -> dict[str, object]:
    return {
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "coverage_mode": "COMPACT",  # 预算 6 单元（BASIC 3 / UNDERSTANDING 2 / DEEP_QUESTION 1）
            "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
        },
    }


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _create_and_start(
    client: TestClient, db_path: Path, *, user: dict[str, str], seed: dict[str, object]
) -> str:
    """V2.5 生命周期推进：创建 DRAFT → 请求样卡 → 显式扫描（样卡 worker）→ start
    → 返回 task_id（进入 GENERATING+PLANNING，供 executor 扫描完成正式生成）。"""
    resp = client.post(
        f"/projects/{seed['project_id']}/tasks",
        json=_payload(seed),
        headers={**user, **_idem()},
    )
    assert resp.status_code == 201
    task_id = str(resp.json()["task_id"])
    assert client.post(f"/tasks/{task_id}/samples", headers={**user, **_idem()}).status_code == 200
    scan_tasks(
        _db_factory(db_path),
        settings=_SETTINGS,
        client_factory=_pipeline_factory(cards=_valid_cards()),
    )
    resp = client.get(f"/tasks/{task_id}", headers=user)
    assert resp.status_code == 200 and resp.json()["status"] == "AWAITING_SAMPLE_CONFIRMATION"
    assert client.post(f"/tasks/{task_id}/start", headers={**user, **_idem()}).status_code == 200
    return task_id


def _valid_cards(n: int = 6) -> list[dict[str, object]]:
    return [{"type": "QUESTION", "question": f"q{i}", "answer": f"a{i}"} for i in range(n)]


def _run_to_completed(
    db_path: Path,
    *,
    cards: list[dict[str, object]],
    scores: dict[str, int] | None = None,
    with_usage: bool = True,
) -> None:
    """显式 executor 扫描一轮（mock transport 全链路）→ 任务 COMPLETED。"""
    n = scan_tasks(
        _db_factory(db_path),
        settings=_SETTINGS,
        client_factory=_pipeline_factory(cards=cards, scores=scores, with_usage=with_usage),
    )
    assert n >= 1


def test_acceptance_ac04_valid_cards_inserted_and_completed(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-a：mock 返回合法卡 → 任务 COMPLETED + 合法卡入库（Schema 通过）。

    新语义载体：6 单元 → 6 批（批=单元）→ 每批 1 卡 → 6 卡入库（旧：2 批 × 3 卡）。
    """
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _create_and_start(client, db_path, user=user, seed=seed)
    _run_to_completed(db_path, cards=_valid_cards())
    body = client.get(f"/tasks/{task_id}", headers=user).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 6  # 6 批 × 1 卡，全部入库
    assert body["total_batch_count"] == 6 and body["completed_batch_count"] == 6
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 6
    assert all(c.source == "GENERATED" for c in cards)
    # AC-04-c 附证：Rubric 分数落库（仅观测），但入库与否由 Schema 决定——合法卡全部入库
    assert all(c.rubric_total_score is not None for c in cards)


def test_acceptance_ac04_invalid_cards_not_inserted_skipped(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-b：mock 返回非法卡（缺 question/answer，generator-output schema v2 违约）
    → 重试预算耗尽 → 批次 SKIPPED 不入库（4.2 批次级失败不中断任务）；但整任务
    无任何有效卡 → 发布阶段整体失败 TASK_ZERO_CARDS（4.1 V25-D-23：0 张有效卡
    整体失败，不显示"完成 0 张"）。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _create_and_start(client, db_path, user=user, seed=seed)
    _run_to_completed(db_path, cards=[{"type": "QUESTION"}])
    body = client.get(f"/tasks/{task_id}", headers=user).json()
    assert body["status"] == "FAILED"  # 0 张有效卡整体失败（4.1 V25-D-23）
    assert body["error_code"] == "TASK_ZERO_CARDS"
    assert body["failure_stage"] == "PUBLISHING"
    assert body["generated_card_count"] == 0
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
    assert cards == []  # 非法卡不入库（Schema 是唯一门槛）
    assert len(batches) == 6  # 每单元一批均耗尽预算 SKIPPED
    assert all(b.status == "SKIPPED" for b in batches)


def test_acceptance_ac04_rubric_no_auto_fix_prune_or_regenerate(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-04-c/d：低分但 Schema 合法的卡照常入库；不自动修复/淘汰/补生成。

    新语义：低分来自 SCORING 阶段（评分 mock evidence/correctness 1 分、
    difficulty 0 分、learning 2 分 → rubric_total_score 4 ≤ 6）；生成响应不含评分，
    入库与否仍只由 Schema 决定。内容按目标序号互异（generation_item_id 防重属
    AC-05，另测）。
    """
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _create_and_start(client, db_path, user=user, seed=seed)
    _run_to_completed(db_path, cards=_valid_cards(), scores=_LOW_SCORES)
    body = client.get(f"/tasks/{task_id}", headers=user).json()
    assert body["status"] == "COMPLETED"
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
        batches = session.scalars(select(Batch).where(Batch.task_id == task_id)).all()
    assert len(cards) == 6  # 低分合法卡全部入库（Rubric 不淘汰）
    assert len(batches) == 6
    assert all(b.status == "SUCCEEDED" for b in batches)
    assert all(b.retry_count == 0 for b in batches)  # 不因低分自动重试/修复
    assert body["total_batch_count"] == 6 and body["completed_batch_count"] == 6  # 不补生成
    assert all(
        c.evidence_score is not None
        and c.correctness_score is not None
        and c.difficulty_score is not None
        and c.learning_value_score is not None
        and c.rubric_total_score is not None
        and c.rubric_total_score <= 6
        for c in cards
    )  # 低分卡仍在牌组（不淘汰），且评分已观测


def test_acceptance_ac07_quality_and_cache_recorded(ctx: tuple[TestClient, Path]) -> None:
    """AC-07-a/b：批次列表含 Rubric/质量/Cache 记录；单卡 Rubric 分数落库。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _create_and_start(client, db_path, user=user, seed=seed)
    _run_to_completed(db_path, cards=_valid_cards())
    resp = client.get(f"/tasks/{task_id}/batches", headers=user)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 6
    for item in items:
        assert item["status"] == "SUCCEEDED"
        # AC-07-b：Prompt Cache 命中/未命中/输出 Token 记录（生成调用 usage 投影）
        assert item["cache_hit_tokens"] == 2
        assert item["cache_miss_tokens"] == 8
        assert item["output_tokens"] == 5
        # AC-07-a：整批质量统计（仅观测，随 rubrics 落库）
        assert item["rubric_version"] == "v3"
        assert item["prompt_version"] == "v4" and item["schema_version"] == "v3"
        assert item["model"] == "deepseek-v4-flash"
        assert item["http_status"] == 200
        assert item["coverage_rate"] == 1.0
        assert item["duplicate_rate"] == 0.0
        assert isinstance(item["difficulty_distribution"], dict)
        assert isinstance(item["chapter_distribution"], dict)
        assert isinstance(item["card_type_distribution"], dict)
        assert item["difficulty_deviation"] == 0.0
        assert item["retry_count"] == 0
        assert item["cost_estimate"] is not None and item["cost_estimate"] > 0  # 仅观测
        # 批=单元：每批恰好 1 个 generation_item_id（旧：每批 3）
        assert isinstance(item["generated_item_ids"], list) and len(item["generated_item_ids"]) == 1
    # AC-07-a：单卡 Rubric 5 分数字段（3.9；经卡片详情核验，此处直读 DB 验证落库）
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 6
    assert all(
        c.evidence_score is not None
        and c.correctness_score is not None
        and c.difficulty_score is not None
        and c.learning_value_score is not None
        and c.rubric_total_score is not None
        and 0 < c.rubric_total_score <= 12
        for c in cards
    )


def test_acceptance_ac07_abnormal_cache_data_does_not_gate_insertion(
    ctx: tuple[TestClient, Path],
) -> None:
    """AC-07-c：Cache 数据异常（usage 缺失 → token 观测 None）不改变既定入库规则。"""
    client, db_path = ctx
    user = _user(client)
    seed = _seed_context(db_path, user_id=_user_id(db_path))
    task_id = _create_and_start(client, db_path, user=user, seed=seed)
    _run_to_completed(db_path, cards=_valid_cards(), with_usage=False)
    body = client.get(f"/tasks/{task_id}", headers=user).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 6  # 入库规则不受 cache 异常影响
    resp = client.get(f"/tasks/{task_id}/batches", headers=user)
    assert resp.status_code == 200
    for item in resp.json()["items"]:
        assert item["status"] == "SUCCEEDED"
        assert item["cache_hit_tokens"] is None  # 异常时观测值 None，不抛错不阻塞
        assert item["cache_miss_tokens"] is None
        assert item["output_tokens"] is None
        assert item["cost_estimate"] is None  # 无 usage 不估算
