"""验收测试：AC-09 牌组与卡片联调（PRD AC-09；走真实迁移 schema + HTTP）。

映射：
- AC-09-1 新建牌组 / 单卡添加 / 批量导入 / 同幂等键重放不重复写入
- AC-09-2 列表与详情展示服务端真实卡片数、待复习数与进度（非本地演示数据）
- AC-09-3 删除牌组后其卡片不再出现在读取结果

补覆盖（V1-T4 审查缺口）：
- import 空 front/back → 422 IMPORT_PARSE_ERROR
- cards GET 跨设备 → 404 DECK_NOT_FOUND
- DELETE 缺 Idempotency-Key → 400 VALIDATION_ERROR
- import 同 key 同 body 幂等重放（首次 results 原样返回）
- handler 级并发幂等：同 (device, path, key) 并发 POST /decks → 一 fresh 一 replay、单副作用

路径无 /v1 前缀——openapi servers url 承担 /v1 语义（与 probes /healthz 同理）。
"""

import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from infra.db.session import create_db_engine

REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/acceptance/ → 仓库根


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """迁移后 schema 的 TestClient（alembic upgrade head → 真实表结构）。"""
    from alembic import command
    from alembic.config import Config

    db_path = tmp_path / "ac09.db"
    cfg = Config(str(REPO_ROOT / "main" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    settings = Settings(
        database_url=f"sqlite:///{db_path}",
        storage_path=tmp_path / "storage",
        # 验收用例单设备请求密集（可 >5 req/s）：限流阈值属可运维调优项（契约 1.6），
        # 测试显式调高避免同秒内第 6 个请求被 429（限流行为由 integration 层专测）
        rate_limit_ip_per_second=1000,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _device() -> dict[str, str]:
    return {"X-Device-ID": str(uuid.uuid4())}


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _create_deck(client: TestClient, device: dict[str, str]) -> str:
    resp = client.post("/decks", json={"name": "D"}, headers={**device, **_idem()})
    assert resp.status_code == 201
    return str(resp.json()["deck_id"])


def test_acceptance_ac09_deck_card_workflow(client: TestClient) -> None:
    """AC-09-1：新建牌组、单卡添加、批量导入；同一幂等键重放不重复写入。"""
    device = _device()
    resp = client.post("/decks", json={"name": "英语"}, headers={**device, **_idem()})
    assert resp.status_code == 201
    deck_id = resp.json()["deck_id"]
    # 单卡添加
    resp = client.post(
        f"/decks/{deck_id}/cards",
        json={"front": "apple", "back": "苹果"},
        headers={**device, **_idem()},
    )
    assert resp.status_code == 201
    # 批量导入（显式持有 (device, import_key) 头变量，供重放复用）
    import_headers = {**device, **_idem()}
    import_body = {"cards": [{"front": "book", "back": "书"}, {"front": "water", "back": "水"}]}
    resp = client.post(f"/decks/{deck_id}/cards/import", json=import_body, headers=import_headers)
    assert resp.status_code == 201
    assert len(resp.json()["results"]) == 2
    # 同一幂等键重复提交 → 重放首次响应，不重复写入
    resp_dup = client.post(
        f"/decks/{deck_id}/cards/import", json=import_body, headers=import_headers
    )
    assert resp_dup.status_code == 201
    assert resp_dup.json() == resp.json()
    cards = client.get(f"/decks/{deck_id}/cards", headers=device).json()["items"]
    assert len(cards) == 3  # 单卡 + 导入 2 张，无重复


def test_acceptance_ac09_real_progress(client: TestClient) -> None:
    """AC-09-2：列表/详情展示服务端真实卡片数、待复习数与进度（非本地演示数据）。"""
    device = _device()
    deck_id = _create_deck(client, device)
    client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**device, **_idem()}
    )
    client.post(
        f"/decks/{deck_id}/cards", json={"front": "f2", "back": "b2"}, headers={**device, **_idem()}
    )
    detail = client.get(f"/decks/{deck_id}", headers=device).json()
    assert detail["card_count"] == 2
    # 新卡初始 due=now（服务端时钟）：due<=查询时刻 now 同值恒真 → 全部到期
    assert detail["due_count"] == 2
    assert detail["mastered_card_count"] == 0
    assert detail["review_count"] == 0
    assert detail["mastery_ratio"] == 0.0
    item = client.get("/decks", headers=device).json()["items"][0]
    assert item["deck_id"] == deck_id
    assert item["card_count"] == 2
    assert item["due_count"] == 2


def test_acceptance_ac09_delete_removes_from_reads(client: TestClient) -> None:
    """AC-09-3：删除牌组后其卡片不再出现在读取结果（详情/列表/卡片列表）。"""
    device = _device()
    deck_id = _create_deck(client, device)
    client.post(
        f"/decks/{deck_id}/cards", json={"front": "f", "back": "b"}, headers={**device, **_idem()}
    )
    resp = client.delete(f"/decks/{deck_id}", headers={**device, **_idem()})
    assert resp.status_code == 204
    resp = client.get(f"/decks/{deck_id}", headers=device)
    assert resp.status_code == 404
    resp = client.get(f"/decks/{deck_id}/cards", headers=device)
    assert resp.status_code == 404
    resp = client.get("/decks", headers=device)
    assert resp.json()["items"] == []


def test_acceptance_ac09_import_empty_front_or_back_422(client: TestClient) -> None:
    """补覆盖：import 卡片 front/back 为空 → 422 IMPORT_PARSE_ERROR（cards.py 手动校验分支）。"""
    device = _device()
    deck_id = _create_deck(client, device)
    for bad in (
        {"cards": [{"front": "", "back": "b"}]},  # front 为空
        {"cards": [{"front": "f", "back": ""}]},  # back 为空
        {"cards": [{"back": "b"}]},  # front 缺失（None 视为空）
    ):
        resp = client.post(
            f"/decks/{deck_id}/cards/import", json=bad, headers={**device, **_idem()}
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "IMPORT_PARSE_ERROR"


def test_acceptance_ac09_cards_cross_device_404(client: TestClient) -> None:
    """补覆盖：cards GET 跨设备 → 404 DECK_NOT_FOUND（资源归属隔离）。"""
    device = _device()
    deck_id = _create_deck(client, device)
    resp = client.get(f"/decks/{deck_id}/cards", headers=_device())
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DECK_NOT_FOUND"


def test_acceptance_ac09_delete_requires_idempotency_key(client: TestClient) -> None:
    """补覆盖：DELETE 缺 Idempotency-Key → 400 VALIDATION_ERROR（写接口强制键，契约 1.3）。"""
    device = _device()
    deck_id = _create_deck(client, device)
    resp = client.delete(f"/decks/{deck_id}", headers=device)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_acceptance_ac09_import_idempotency_replay(client: TestClient) -> None:
    """补覆盖：import 同 key 同 body 幂等重放 → 首次 results 原样返回、不重复写入。"""
    device = _device()
    key = _idem()
    deck_id = _create_deck(client, device)
    headers = {**device, **key}
    body = {"cards": [{"front": "book", "back": "书"}, {"front": "water", "back": "水"}]}
    resp1 = client.post(f"/decks/{deck_id}/cards/import", json=body, headers=headers)
    assert resp1.status_code == 201
    resp2 = client.post(f"/decks/{deck_id}/cards/import", json=body, headers=headers)
    assert resp2.status_code == 201
    assert resp2.json() == resp1.json()  # 重放首次 results（同一批 card_id）
    cards = client.get(f"/decks/{deck_id}/cards", headers=device).json()["items"]
    assert len(cards) == 2  # 单副作用


def test_acceptance_ac09_concurrent_idempotency_single_side_effect(
    client: TestClient, tmp_path: Path
) -> None:
    """补覆盖：handler 级并发幂等——两个线程同 (device, path, key) 并发 POST /decks。

    两个独立 TestClient（同库）经 threading.Barrier 同时发请求：唯一约束占位，
    后到事务 BEGIN IMMEDIATE 串行化 → 回滚重读重放。HTTP 响应层面无法直接区分
    fresh/replay（两者 body 相同），以组合断言证明恰一 fresh 一 replay：
    两次均 201 且 deck_id 相同 + 幂等记录仅 1 行 + 牌组列表只有 1 个（单副作用）。
    """
    device_id = str(uuid.uuid4())
    headers = {"X-Device-ID": device_id, "Idempotency-Key": str(uuid.uuid4())}
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'ac09.db'}",
        storage_path=tmp_path / "storage",
        rate_limit_ip_per_second=1000,  # 与 fixture 同口径：并发请求免遭 IP 限流干扰
    )
    with TestClient(create_app(settings)) as client2:
        barrier = threading.Barrier(2)
        statuses: list[int] = []
        deck_ids: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(c: TestClient) -> None:
            barrier.wait(timeout=10)
            try:
                resp = c.post("/decks", json={"name": "D"}, headers=headers)
                with lock:
                    statuses.append(resp.status_code)
                    deck_ids.append(str(resp.json()["deck_id"]))
            except Exception as exc:
                # 记录并重抛（与 integration 并发测试同模式）：断言 errors==[] 暴露线程异常
                with lock:
                    errors.append(exc)
                raise

        threads = [
            threading.Thread(target=worker, args=(client,)),
            threading.Thread(target=worker, args=(client2,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

    assert errors == []
    assert statuses == [201, 201]
    assert len(set(deck_ids)) == 1  # 重放返回首次 deck_id（fresh 若重复执行必生成不同 id）
    resp = client.get("/decks", headers={"X-Device-ID": device_id})
    assert len(resp.json()["items"]) == 1  # 单副作用：牌组列表只有 1 个
    engine = create_db_engine(f"sqlite:///{tmp_path / 'ac09.db'}")
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() or 0
    assert rows == 1  # 幂等记录仅一行 → 恰一个 fresh 占位，其余为重放
