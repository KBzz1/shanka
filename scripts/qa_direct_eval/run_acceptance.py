"""run_acceptance.py：问答直通模式（QA_DIRECT，V25-D-43）真实验收。

流程（生产 HTTP 链路，本地服务 127.0.0.1:8000）：专用验收账号登录 → PUT /api-key
（.env 的 DEEPSEEK_API_KEY）→ 空项目 → 上传题库 markdown 资料（V25-D-40 端点，同步就绪）
→ 如需确认章节 → 全章节建 QA_DIRECT 任务 → 样卡 → start → AWAITING_CONFIRMATION →
confirm 发布 → 导出卡组全部卡片与任务元数据到 run/<ts>/。

产出供 build_judge_payloads.py（双裁判盲评输入）与 make_report.py（榜单聚合）消费。
凭据只从 .env 读取，不写入命令行参数、不落任何输出。

用法：
    conda run -n shanka-backend python scripts/qa_direct_eval/run_acceptance.py \
        [--base-url http://127.0.0.1:8000] [--fixtures-dir <dir>]
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = Path(__file__).resolve().parent
EMAIL = "qa-direct-eval@shanka.test"
PASSWORD = "QA-direct-eval-2026!"
RATIO = {"basic": 100, "understanding": 0, "deep_question": 0}


def _load_env_key() -> str:
    env = REPO_ROOT / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(".env 缺少 DEEPSEEK_API_KEY")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _wait_task_status(
    client: httpx.Client, headers: dict, task_id: str, targets: set[str], timeout_s: int
) -> dict:
    deadline = time.time() + timeout_s
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/tasks/{task_id}", headers=headers).json()
        status = body.get("status")
        if status in targets:
            return body
        if status == "FAILED":
            raise SystemExit(
                f"任务 FAILED：{body.get('error_code')} / {body.get('error_message')}"
            )
        time.sleep(3)
    raise SystemExit(
        f"任务轮询超时（最后状态 {body.get('status')} / {body.get('internal_stage')}）"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--fixtures-dir", default=str(WORKSPACE / "fixtures"))
    args = parser.parse_args()
    fixtures = Path(args.fixtures_dir)
    bank_path = fixtures / "qa_bank.md"
    gt_path = fixtures / "ground_truth.json"

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = WORKSPACE / "run" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    api_key = _load_env_key()
    client = httpx.Client(base_url=args.base_url, trust_env=False, timeout=120.0)
    t0 = time.monotonic()

    # 1. 登录（不存在则注册）
    login = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if login.status_code != 200:
        reg = client.post(
            "/auth/register",
            json={
                "username": "qa-direct-eval",
                "email": EMAIL,
                "password": PASSWORD,
                "password_confirmation": PASSWORD,
            },
        )
        assert reg.status_code in (200, 201, 400), reg.text  # 400 = 已注册
        login = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    print("[1/8] 验收账号登录 OK")

    # 2. 保存 API Key（掩码返回，不入日志）
    resp = client.put(
        "/api-key", json={"api_key": api_key}, headers={**headers, **_idem()}
    )
    assert resp.status_code == 200, resp.text
    print(f"[2/8] API Key 已保存（状态 {resp.json().get('status')}）")

    # 3. 空项目（两步创建第一步）
    project = client.post(
        "/projects",
        json={"name": f"QA 直通验收 {run_id}"},
        headers={**headers, **_idem()},
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["project_id"]
    print(f"[3/8] 项目 {project_id[:8]} 创建")

    # 4. 上传题库 markdown 资料（同步解析、即时就绪）
    resp = client.post(
        f"/projects/{project_id}/materials/markdown",
        files={"file": ("qa_bank.md", bank_path.read_bytes(), "text/markdown")},
        headers={**headers, **_idem()},
    )
    assert resp.status_code == 201, resp.text
    body = client.get(f"/projects/{project_id}", headers=headers).json()
    if body.get("status") == "AWAITING_CHAPTER_CONFIRMATION":
        body = client.post(
            f"/projects/{project_id}/confirm-chapters", headers={**headers, **_idem()}
        ).json()
    assert body.get("status") == "READY", body.get("status")
    chapters = body.get("chapters") or []
    assert chapters, "资料无章节"
    chapter_ids = [c["chapter_id"] for c in chapters]
    print(
        f"[4/8] 题库资料就绪（{len(chapters)} 章："
        + "、".join(f"{c['name']}({c.get('source')})" for c in chapters)
        + "）"
    )

    # 5. 牌组 + QA_DIRECT 任务
    deck = client.post(
        "/decks",
        json={"name": f"QA 直通验收 {run_id}", "project_id": project_id},
        headers={**headers, **_idem()},
    ).json()
    task = client.post(
        f"/projects/{project_id}/tasks",
        json={
            "deck_id": deck["deck_id"],
            "chapter_ids": chapter_ids,
            "generation_config": {
                "coverage_mode": "BALANCED",
                "difficulty_ratio": RATIO,
                "source_mode": "QA_DIRECT",
            },
        },
        headers={**headers, **_idem()},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["task_id"]
    print(f"[5/8] 任务 {task_id[:8]}（source_mode=QA_DIRECT，{len(chapter_ids)} 章）")

    # 6. 样卡 → 确认
    t_samples = time.monotonic()
    assert (
        client.post(
            f"/tasks/{task_id}/samples", headers={**headers, **_idem()}
        ).status_code
        == 200
    )
    sample_body = _wait_task_status(
        client, headers, task_id, {"AWAITING_SAMPLE_CONFIRMATION"}, 600
    )
    print(
        f"[6/8] 样卡就绪（{len(sample_body.get('sample_cards') or [])} 张，"
        f"{time.monotonic() - t_samples:.0f}s）"
    )

    # 7. start → 生成 → confirm 发布
    t_gen = time.monotonic()
    assert (
        client.post(
            f"/tasks/{task_id}/start", headers={**headers, **_idem()}
        ).status_code
        == 200
    )
    _wait_task_status(client, headers, task_id, {"AWAITING_CONFIRMATION"}, 3600)
    print(
        f"[7/8] 生成完成（AWAITING_CONFIRMATION，{time.monotonic() - t_gen:.0f}s）→ confirm"
    )
    final = client.post(
        f"/tasks/{task_id}/confirm", headers={**headers, **_idem()}
    ).json()
    assert final.get("status") == "COMPLETED", final

    # 8. 导出卡片与元数据
    cards_resp = client.get(
        f"/decks/{deck['deck_id']}/cards", params={"order": "position"}, headers=headers
    ).json()
    card_items = (
        cards_resp if isinstance(cards_resp, list) else cards_resp.get("items", [])
    )
    elapsed = time.monotonic() - t0
    dist: dict[str, int] = {}
    for card in card_items:
        dist[card.get("card_type") or "?"] = (
            dist.get(card.get("card_type") or "?", 0) + 1
        )
    scored = sum(1 for c in card_items if c.get("rubric_total_score") is not None)

    (run_dir / "cards.json").write_text(
        json.dumps(card_items, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "base_url": args.base_url,
                "task_id": task_id,
                "project_id": project_id,
                "deck_id": deck["deck_id"],
                "chapters": [
                    {
                        "chapter_id": c["chapter_id"],
                        "name": c["name"],
                        "source": c.get("source"),
                        "start_page": c.get("start_page"),
                        "end_page": c.get("end_page"),
                    }
                    for c in chapters
                ],
                "generation_config": {
                    "coverage_mode": "BALANCED",
                    "difficulty_ratio": RATIO,
                    "source_mode": "QA_DIRECT",
                },
                "ground_truth_file": str(gt_path),
                "fixture_bank": str(bank_path),
                "generated_card_count": final.get("generated_card_count"),
                "card_type_distribution": dist,
                "rubric_scored_cards": scored,
                "elapsed_seconds": round(elapsed, 1),
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"[8/8] 任务 COMPLETED：{len(card_items)} 卡（{dist}），评分覆盖 {scored}/{len(card_items)}"
        f"，全程 {elapsed:.0f}s"
    )
    print(f"产出：{run_dir / 'cards.json'}")
    print(f"后续：python scripts/qa_direct_eval/build_judge_payloads.py --run {run_id}")
    print(
        f"      conda run -n shanka-backend python scripts/task_quality_report.py --task-id {task_id}"
    )


if __name__ == "__main__":
    main()
