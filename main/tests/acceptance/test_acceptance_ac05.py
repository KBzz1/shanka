"""验收测试：AC-05 任务恢复与幂等（PRD 9；迁移 schema + HTTP + mock transport + 崩溃模拟）。

LLM 升级管线（规划 → 生成 → 评分）mock 全链路驱动；AC-05 恢复意图在新语义下的映射：

AC-05-a 中断后已入库卡保留 → 批 2 chat 前 SystemExit 崩溃（T1 模式）后：任务停留
        RUNNING、批 1 SUCCEEDED + 1 卡落库（批次事务粒度——崩溃不丢已完成批次）；
        批 2 抢占 + STARTED 占位已随调用前事务提交（spec §9）→ PROCESSING 保留
        （旧语义 claim 随崩溃回滚 → PENDING；新语义经心跳超时孤儿恢复复位）
AC-05-b 继续任务从游标继续 → resume（孤儿 RUNNING 抢占）→ 恢复扫描只处理批 2..6
        → COMPLETED；completed_batch_count 1 → 6（游标原子推进）
AC-05-c 已完成批次不重复执行 → 恢复后批 1 仍 SUCCEEDED（retry_count=0），其生成
        调用恰好 1 次（学习目标序列断言 + 账本 attempt_count==1）
AC-05-d generation_item_id 不重复入库 → 恢复/重入边缘：批 2 将生成的卡（seed 可复算）
        预先已在库（等价旧语义"批 2 响应含批内重复内容"——新 pipeline 每批恰好 1 卡，
        重复只能经同 seed 重入出现）→ 只入库 1 张（dedup 命中）+
        duplicate_rate 1.0 观测 + 全任务卡 generation_item_id 互异
场景 2（取消保留）：任务运行中 cancel → CANCELLED + 已入库卡保留（1 卡不动），取消后不再处理

后台循环间隔拉大（3600s）隔离：显式调 executor.scan_once（test_tasks_api 同款"显式
scan_once"模式）；崩溃模拟 = mock transport 指定调用抛 SystemExit（BaseException——绕过
executor 的 except Exception，等价进程崩溃）。
"""

import json
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.main import create_app
from infra.db.models import ApiKey, Batch, Card, Chapter, Device, LlmCallAttempt, PdfFile, Task
from infra.db.session import create_db_engine, create_session_factory
from infra.llm.crypto import encrypt_key, key_from_settings
from infra.llm.deepseek import DeepSeekClient
from services.decks.service import create_deck
from services.generation.batches import _stable_uuid
from services.pdf.text_chunks import persist_text_chunks
from services.tasks.executor import scan_once as scan_tasks

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根

_SETTINGS = Settings(api_key_encryption_key="aa" * 32)
_TEST_ENCRYPTION_KEY = key_from_settings(_SETTINGS)
assert _TEST_ENCRYPTION_KEY is not None
_ENCRYPTED_TEST_KEY = encrypt_key("sk-test-abc", _TEST_ENCRYPTION_KEY)


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[tuple[TestClient, Path, Settings]]:
    """迁移后 schema 的 TestClient（后台任务循环隔离：间隔 3600s）+ DB 路径 + 应用 settings。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac05.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        task_scan_interval_seconds=3600.0,  # 测试不依赖后台循环，显式 scan_once
    )
    with TestClient(create_app(settings)) as client:
        yield client, db_path, settings


def _uuid() -> str:
    return str(uuid.uuid4())


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _seed_context(db_path: Path, *, device_id: str) -> dict[str, object]:
    """devices 前置 + PDF(PARSED) + 2 章节 + 牌组 + 真实加密 Key（executor 解密路径）
    + 页文本（text_chunks——LLM 升级管线规划输入）。"""
    factory = create_session_factory(create_db_engine(f"sqlite:///{db_path}"))
    with factory() as session:
        session.add(Device(device_id=device_id, created_at="2026-08-11T00:00:00.000Z"))
        session.flush()
        pdf = PdfFile(
            file_id=_uuid(),
            device_id=device_id,
            filename="b.pdf",
            storage_key=_uuid(),
            size_bytes=10,
            status="PARSED",
            created_at="2026-08-11T00:00:00.000Z",
        )
        session.add(pdf)
        session.flush()
        deck = create_deck(session, device_id=device_id, name="D", now="2026-08-11T00:00:00.000Z")
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
        session.add(
            ApiKey(
                device_id=device_id,
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
    return {"file_id": pdf.file_id, "deck_id": deck.deck_id, "chapter_ids": chapter_ids}


def _payload(seed: dict[str, object]) -> dict[str, object]:
    return {
        "file_id": seed["file_id"],
        "deck_id": seed["deck_id"],
        "chapter_ids": seed["chapter_ids"],
        "generation_config": {
            "quantity_tendency": "COMPACT",  # 预算 6 单元（BASIC 3 / UNDERSTANDING 2 / APPLICATION 1）
            "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
        },
    }


def _db_factory(db_path: Path) -> sessionmaker[Session]:
    return create_session_factory(create_db_engine(f"sqlite:///{db_path}"))


def _scripted_factory(
    calls: dict[str, int],
    gen_objectives: list[str],
    *,
    crash_call: int,
) -> Callable[[str], DeepSeekClient]:
    """mock transport 全链路分派（planner → generator → scorer）+ 崩溃注入。

    - <PLANNER_INPUT>：按请求配额产出锚定单元（学习目标全局唯一编号——生成调用可按
      目标定位批次）；2 章 → 2 次规划调用 → 6 单元（知识点0..5）。
    - <GENERATOR_INPUT>：crash_call 次生成调用抛 SystemExit（崩溃模拟——批 2 处理
      中断）；其余按学习目标序号返回 q{i}/a{i}（记录 gen_objectives 供"批 1 未重跑"
      断言）。
    - <SCORING_INPUT>：ID 守恒的确定性分数（总分代码计算 9）。

    崩溃后新 client（重启模拟）经同一 factory/calls 计数从 crash_call + 1 次继续。
    """
    counter = [0]

    def factory(api_key: str) -> DeepSeekClient:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            user = body["messages"][-1]["content"]
            calls["n"] += 1
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
            else:  # 生成调用：每批 1 卡（按学习目标序号）
                payload = json.loads(
                    user.split("<GENERATOR_INPUT>", 1)[1].split("</GENERATOR_INPUT>", 1)[0]
                )
                objective = payload["learning_objective"]
                gen_objectives.append(cast(str, objective))
                if calls["n"] == crash_call:
                    raise SystemExit("模拟崩溃：批 2 处理中断")
                index = int(cast(str, objective).split("知识点", 1)[1])
                content = json.dumps(
                    {
                        "cards": [
                            {"type": "QUESTION", "question": f"q{index}", "answer": f"a{index}"}
                        ]
                    },
                    ensure_ascii=False,
                )
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": content}}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "prompt_cache_hit_tokens": 2,
                        "prompt_cache_miss_tokens": 8,
                    },
                    "model": "deepseek-v4-flash",
                },
            )

        return DeepSeekClient(_SETTINGS, transport=httpx.MockTransport(handler))

    return factory


def _batches(session: Session, task_id: str) -> list[Batch]:
    return list(
        session.scalars(
            select(Batch).where(Batch.task_id == task_id).order_by(Batch.batch_index)
        ).all()
    )


def test_acceptance_ac05_crash_resume_cursor_and_dedup(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """AC-05 a-d：批 2 前崩溃 → 卡保留 + resume 孤儿从游标继续 + 批 1 不重跑 +
    generation_item_id 防重。"""
    client, db_path, settings = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # 崩溃模拟（T1 模式）：扫描 = 2 次规划（2 章）+ 批 1 一次生成成功，批 2 前
    # SystemExit（绕过 executor 的 except Exception）；崩溃点 = 第 4 次调用
    calls: dict[str, int] = {"n": 0}
    gen_objectives: list[str] = []
    factory = _scripted_factory(calls, gen_objectives, crash_call=4)
    with pytest.raises(SystemExit):
        scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert calls["n"] == 4  # 2 规划 + 批 1 + 批 2 崩溃
    assert gen_objectives == ["知识点0", "知识点1"]  # 崩溃前生成序（批 1、批 2）

    # AC-05-a：崩溃后任务停留 RUNNING + 批 1 SUCCEEDED + 卡保留（批次事务粒度已落库）；
    # 批 2 抢占 + STARTED 占位已随调用前事务提交（spec §9）→ PROCESSING
    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "RUNNING"
    assert body["generated_card_count"] == 1
    assert body["completed_batch_count"] == 1 and body["total_batch_count"] == 6
    with _db_factory(db_path)() as session:
        task = session.get(Task, task_id)
        assert task is not None
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
        batch1_attempts = session.scalars(
            select(LlmCallAttempt).where(
                LlmCallAttempt.task_id == task_id,
                LlmCallAttempt.stage == "GENERATING",
                LlmCallAttempt.operation_key == f"generating:{batches[0].batch_id}",
            )
        ).all()
    assert [b.status for b in batches] == [
        "SUCCEEDED",
        "PROCESSING",
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
    ]  # 批 2 claim/STARTED 已提交（§9），恢复经心跳超时孤儿复位
    assert len(cards) == 1  # 已入库卡保留（AC-05-a）
    assert [a.status for a in batch1_attempts] == ["SUCCESS"]  # 批 1 账本一次成功（AC-05-c 前置）

    # 新鲜 RUNNING（心跳内）resume → 409（孤儿判据生效，非 PAUSED 路径）
    resp = client.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "TASK_STATE_CONFLICT"

    # 模拟 30 分钟孤儿窗口流逝（心跳超时）：updated_at 回拨到足够过去（孤儿判据 = 心跳超时）
    with _db_factory(db_path)() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.updated_at = "2026-07-01T00:00:00.000Z"
        session.commit()

    # 新 app（重启模拟）→ resume（孤儿）→ 200 RUNNING
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get(f"/tasks/{task_id}", headers=device).json()["status"] == "RUNNING"
        resp = restarted.post(f"/tasks/{task_id}/resume", headers={**device, **_idem()})
        assert resp.status_code == 200
        assert resp.json()["status"] == "RUNNING"

    # AC-05-d 防重前置：模拟恢复/重入边缘——批 2 将生成的卡（seed = gen|task|2|QUESTION|q1|a1
    # 可复算）已在库（等价旧语义"批 2 响应含批内重复内容"：新 pipeline 每批恰好 1 卡，
    # 内容重复只能经同 seed 重入出现，防重守卫 = generation_item_id 先查后插）
    dup_gen_item = _stable_uuid(f"gen|{task_id}|2|QUESTION|q1|a1")
    with _db_factory(db_path)() as session:
        task_row = session.get(Task, task_id)
        assert task_row is not None and task_row.deck_id is not None
        session.add(
            Card(
                card_id=_uuid(),
                deck_id=task_row.deck_id,
                device_id=device["X-Device-ID"],
                source="GENERATED",
                position=2,
                front="q1",
                back="a1",
                card_type="QUESTION",
                question="q1",
                answer="a1",
                generation_item_id=dup_gen_item,
                target_difficulty="BASIC",
                version="v1",
                created_at="2026-08-11T00:00:00.000Z",
                updated_at="2026-08-11T00:00:00.000Z",
            )
        )
        session.commit()

    # resume 会刷新心跳（updated_at=now）；GENERATING 孤儿恢复按心跳超时判据——
    # 恢复扫描前再次回拨，模拟恢复 worker 接管前的孤儿窗口流逝
    with _db_factory(db_path)() as session:
        task = session.get(Task, task_id)
        assert task is not None
        task.updated_at = "2026-07-01T00:00:00.000Z"
        session.commit()

    # AC-05-b：恢复扫描从游标继续——只处理批 2..6 → COMPLETED（同一 transport 继续调用）
    n = scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert n == 1
    assert calls["n"] == 14  # 2 规划 + 7 生成（批 2 崩溃后重试一次）+ 5 评分组
    # （评分分层：BASIC×2 章 + UNDERSTANDING×2 章 各一组、APPLICATION 逐单元 1 组）
    # AC-05-c：批 1 未重跑（重跑会再次出现 知识点0 生成调用）；批 2 崩溃+恢复共 2 次
    assert gen_objectives == [
        "知识点0",
        "知识点1",
        "知识点1",
        "知识点2",
        "知识点3",
        "知识点4",
        "知识点5",
    ]
    assert gen_objectives.count("知识点0") == 1  # 批 1 未重跑（AC-05-c）
    assert gen_objectives.count("知识点1") == 2  # 崩溃 + 孤儿恢复重试

    body = client.get(f"/tasks/{task_id}", headers=device).json()
    assert body["status"] == "COMPLETED"
    assert body["generated_card_count"] == 5  # 批 1 + 批 3..6 新入库（批 2 dedup 命中不增计数）
    assert body["completed_batch_count"] == 6 and body["total_batch_count"] == 6  # 游标到终值
    with _db_factory(db_path)() as session:
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert [b.status for b in batches] == ["SUCCEEDED"] * 6
    assert batches[0].retry_count == 0  # 批 1 未重跑
    assert batches[1].retry_count == 1  # 批 2：1 次崩溃尝试（UNKNOWN 计预算）投影
    assert all(b.retry_count == 0 for b in batches[2:])  # 批 3..6 一次成功
    assert len(cards) == 6  # 批 1 卡 + 批 2 dedup 命中的既有卡 + 批 3..6 卡
    assert all(c.generation_item_id is not None for c in cards)
    assert len({c.generation_item_id for c in cards}) == len(cards)  # AC-05-d：无重复入库
    assert sum(1 for c in cards if c.generation_item_id == dup_gen_item) == 1  # 防重唯一

    # AC-05-d 附证（批次视图观测）：批 2 重复内容被防重命中 → duplicate_rate 1.0，
    # 批 1/批 2 generated_item_ids 各 1（批=单元）
    items = client.get(f"/tasks/{task_id}/batches", headers=device).json()["items"]
    assert (
        isinstance(items[0]["generated_item_ids"], list)
        and len(items[0]["generated_item_ids"]) == 1
    )
    assert items[1]["generated_item_ids"] == [dup_gen_item]
    assert items[1]["duplicate_rate"] == 1.0
    assert items[0]["duplicate_rate"] == 0.0


def test_acceptance_ac05_cancel_keeps_inserted_cards(
    ctx: tuple[TestClient, Path, Settings],
) -> None:
    """场景 2（取消保留）：任务运行中 cancel → CANCELLED + 已入库卡保留，取消后不再处理。"""
    client, db_path, _ = ctx
    device = _device()
    seed = _seed_context(db_path, device_id=device["X-Device-ID"])
    resp = client.post("/tasks", json=_payload(seed), headers={**device, **_idem()})
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # 任务运行中：批 1 成功后崩溃暂停（T1 模式）→ 任务 RUNNING + 1 卡已入库
    calls: dict[str, int] = {"n": 0}
    gen_objectives: list[str] = []
    factory = _scripted_factory(calls, gen_objectives, crash_call=4)
    with pytest.raises(SystemExit):
        scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory)
    assert calls["n"] == 4

    resp = client.post(f"/tasks/{task_id}/cancel", headers={**device, **_idem()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CANCELLED"
    assert body["ended_at"] is not None
    assert body["generated_card_count"] == 1  # 视图仍报告已入库卡（保留）

    # 已入库卡保留：批 1 SUCCEEDED + 1 卡不动（取消不回滚已完成批次）；
    # 批 2 抢占 + STARTED 占位已提交（spec §9）→ PROCESSING
    with _db_factory(db_path)() as session:
        batches = _batches(session, task_id)
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert [b.status for b in batches] == [
        "SUCCEEDED",
        "PROCESSING",
        "PENDING",
        "PENDING",
        "PENDING",
        "PENDING",
    ]
    assert len(cards) == 1

    # 取消后不再处理：CANCELLED 不进入扫描（无 RUNNING 任务），无新 chat 调用、卡数不变
    assert scan_tasks(_db_factory(db_path), settings=_SETTINGS, client_factory=factory) == 0
    assert calls["n"] == 4
    with _db_factory(db_path)() as session:
        cards = session.scalars(
            select(Card).where(Card.deck_id == cast(str, seed["deck_id"]))
        ).all()
    assert len(cards) == 1
