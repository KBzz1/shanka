"""run_b5_acceptance.py：密度制真实验收（B5 门禁，V25-D-26）。

流程（生产 HTTP 链路，本地服务 127.0.0.1:8000）：专用验收账号登录 → PUT /api-key（.env
的 DEEPSEEK_API_KEY）→ 上传样书建项目 → 轮询解析 → 选第 1 章 COMPACT 建任务 → 样卡 →
start → 轮询终态 → 打印数量/难度分布验收证据。凭据只从 .env 读取，不写入命令行参数。

用法：
    conda run -n shanka-backend python scripts/run_b5_acceptance.py [--base-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = REPO_ROOT / "res" / "AI-Agents-in-Depth-zh-CN.pdf"
EMAIL = "b5-acceptance@shanka.test"
PASSWORD = "B5-acceptance-2026!"
RATIO = {"basic": 40, "understanding": 40, "deep_question": 20}


def _load_env_key() -> str:
    env = REPO_ROOT / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("DEEPSEEK_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(".env 缺少 DEEPSEEK_API_KEY")


def _idem() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


def _wait_project_parsed(client: httpx.Client, headers: dict, project_id: str, timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/projects/{project_id}", headers=headers)
        body = resp.json()
        status = body.get("status")
        if status in ("AWAITING_CHAPTER_CONFIRMATION", "READY"):
            return body
        if status == "PARSE_FAILED":
            raise SystemExit(f"解析失败：{body.get('file', {}).get('error_code')}")
        time.sleep(2)
    raise SystemExit("解析轮询超时")


def _wait_task_status(client: httpx.Client, headers: dict, task_id: str, targets: set[str], timeout_s: int) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get(f"/tasks/{task_id}", headers=headers).json()
        if body.get("status") in targets:
            return body
        time.sleep(3)
    raise SystemExit(f"任务轮询超时（最后状态 {body.get('status')}）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    api_key = _load_env_key()
    client = httpx.Client(base_url=args.base_url, trust_env=False, timeout=120.0)

    # 1. 登录（不存在则注册）
    login = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    if login.status_code != 200:
        reg = client.post(
            "/auth/register",
            json={"username": "b5-acceptance", "email": EMAIL, "password": PASSWORD,
                  "password_confirmation": PASSWORD},
        )
        assert reg.status_code in (200, 201, 400), reg.text  # 400 = 已注册
        login = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("[1/6] 验收账号登录 OK")

    # 2. 保存 API Key（掩码返回，不入日志）
    resp = client.put("/api-key", json={"api_key": api_key}, headers={**headers, **_idem()})
    assert resp.status_code == 200, resp.text
    print(f"[2/6] API Key 已保存（状态 {resp.json().get('status')}）")

    # 3. 上传样书建项目（幂等键固定：重跑不重复建项目）
    idem_upload = _idem()["Idempotency-Key"]
    with SAMPLE.open("rb") as f:
        resp = client.post(
            "/projects",
            files={"file": ("AI-Agents-in-Depth-zh-CN.pdf", f, "application/pdf")},
            data={"name": "B5 密度制验收"},
            headers={**headers, "Idempotency-Key": idem_upload},
        )
    if resp.status_code == 409:
        # 幂等重放：查回已有项目
        projects = client.get("/projects", headers=headers).json()["items"]
        project_id = next(p["project_id"] for p in projects if p["name"] == "B5 密度制验收")
    else:
        assert resp.status_code == 201, resp.text
        project_id = resp.json()["project_id"]
    print(f"[3/6] 项目 {project_id[:8]} 就绪，轮询解析…")
    project = _wait_project_parsed(client, headers, project_id, timeout_s=600)

    # 4. 选第 1 章（引言/前言之后的第一个正文章节；没有则取第一个）
    chapters = project["file"]["chapters"]
    assert chapters, "无章节"
    chapter = next(
        (c for c in chapters if c["name"].lstrip().startswith(("第 1 章", "第1章", "第一章"))),
        chapters[0],
    )
    print(f"[4/6] 选章：{chapter['name']}（页 {chapter['start_page']}-{chapter['end_page']}）")

    deck = client.post("/decks", json={"name": "B5 密度制验收", "project_id": project_id},
                       headers={**headers, **_idem()}).json()
    task = client.post(
        f"/projects/{project_id}/tasks",
        json={"deck_id": deck["deck_id"], "chapter_ids": [chapter["chapter_id"]],
              "generation_config": {"coverage_mode": "COMPACT", "difficulty_ratio": RATIO}},
        headers={**headers, **_idem()},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["task_id"]

    # 5. 样卡 → 确认 → 正式生成
    assert client.post(f"/tasks/{task_id}/samples", headers={**headers, **_idem()}).status_code == 200
    sample_body = _wait_task_status(client, headers, task_id, {"AWAITING_SAMPLE_CONFIRMATION", "FAILED"}, 600)
    assert sample_body["status"] == "AWAITING_SAMPLE_CONFIRMATION", sample_body
    print(f"[5/6] 样卡就绪（{len(sample_body.get('sample_cards', []))} 张）→ start")
    assert client.post(f"/tasks/{task_id}/start", headers={**headers, **_idem()}).status_code == 200
    final = _wait_task_status(client, headers, task_id, {"COMPLETED", "FAILED"}, 3600)
    assert final["status"] == "COMPLETED", final
    cards = client.get(f"/decks/{deck['deck_id']}/cards",
                       params={"order": "position"}, headers=headers).json()
    card_items = cards if isinstance(cards, list) else cards.get("items", [])
    dist = {"BASIC": 0, "UNDERSTANDING": 0, "DEEP_QUESTION": 0}
    for card in card_items:
        dist[card.get("target_difficulty") or "BASIC"] += 1
    print("[6/6] 任务 COMPLETED")
    print(json.dumps({
        "task_id": task_id,
        "chapter": chapter["name"],
        "generated_card_count": final.get("generated_card_count"),
        "difficulty_distribution": dist,
        "target_ratio_pct": RATIO,
    }, ensure_ascii=False, indent=2))
    print("后续：python scripts/task_quality_report.py --task-id", task_id)


if __name__ == "__main__":
    main()
