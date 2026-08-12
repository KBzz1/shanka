"""完整制卡流程场景(API Key → PDF → 样卡 → 任务 → 复习 → 看板)端到端联调。

真实 Key 消耗 3 次 LLM 调用(api-key 校验 1 + samples 1 + tasks 1),LLM_CALLS=3
供 runner 成本统计;API Key 从仓库根 .env 读取,仅内存使用,绝不输出。
运行方式(由 runner 调度或直接):
    python3 scenarios/flow/live_flow.py --base-url http://localhost:8000
    python3 scenarios/flow/live_flow.py --skip-generate   # 到样卡为止,省 1 次 LLM 调用
退出码 = 失败步骤数(0 = 全部通过)。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import uuid
from pathlib import Path

# 场景模块被直接执行时 sys.path[0] 是脚本所在目录(scenarios/flow),
# 把 test-platform 根放入搜索路径以支持 `python3 scenarios/flow/live_flow.py`
_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shanka.cleanup import DataScope
from shanka.client import Response, ShankaClient
from shanka.report import check, summary

NAME = "live_flow"
SUITE = "flow"
LLM_CALLS = 3  # api-key 校验 1 + samples 1 + tasks 1

_ENV_FILE = Path("/home/kbzz1/shanka_backend/.env")  # 仓库根 .env(密钥仅内存使用)
_POLL_INTERVAL_S = 5    # 任务轮询间隔
_POLL_TIMEOUT_S = 600   # 任务轮询总上限
_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")
_GEN_CONFIG = {
    "quantity_tendency": "BALANCED",
    "difficulty_ratio": {"basic": 0.4, "understanding": 0.4, "application": 0.2},
}


def _body(r: Response) -> dict:
    """安全取响应 JSON 字典(非 dict/解析失败时返回空字典)。"""
    return r.json if isinstance(r.json, dict) else {}


def _sample_cards(body: dict) -> list[dict]:
    """样卡数组兼容多字段名(samples/sample_cards/cards/items/data)。"""
    for key in ("samples", "sample_cards", "cards", "items", "data"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return []


def _load_env_key() -> str:
    """从 .env 正则解析 DEEPSEEK_API_KEY;缺失报错退出(密钥不入日志/print/命令行)。"""
    try:
        text = _ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        raise SystemExit(f"无法读取 {_ENV_FILE}(缺失 DEEPSEEK_API_KEY)") from None
    for line in text.splitlines():
        m = re.match(r"DEEPSEEK_API_KEY\s*=\s*(.+)", line.strip())
        if m:
            key = m.group(1).strip().strip('"').strip("'")
            if key:
                return key
    raise SystemExit(f"未在 {_ENV_FILE} 找到 DEEPSEEK_API_KEY")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--device-id", default=None, help="固定 X-Device-ID(默认随机)")
    ap.add_argument("--skip-generate", action="store_true", help="跳过 POST /tasks 与评级,到样卡为止")
    ap.add_argument("--keep", action="store_true", help="不清理创建的牌组")
    args = ap.parse_args(argv)

    # 密钥仅内存使用;client 对 PUT /api-key 自动脱敏不记录(红线 4)
    api_key = _load_env_key()
    # 任务生成期后端事件循环阻塞,轮询请求需长超时(默认 30s 不足)
    c = ShankaClient(args.base_url, device_id=args.device_id, timeout=60)
    scope = DataScope(c)

    # 1. API Key 保存(幂等)与状态
    r = c.request("PUT", "/api-key", body={"api_key": api_key}, idempotent=True, step="api-key-put")
    check("PUT /api-key -> 200", r.status == 200, f"({r.status})")
    r = c.request("GET", "/api-key/status", step="api-key-status")
    body = _body(r)
    check("GET /api-key/status -> AVAILABLE", body.get("status") == "AVAILABLE",
          f"status={body.get('status')}")

    # 2. 复用已解析 PDF:列表选第一个 PARSED,取章节断言非空
    r = c.request("GET", "/pdfs", step="pdf-list")
    check("GET /pdfs -> 200", r.status == 200, f"({r.status})")
    pdfs = [p for p in _body(r).get("items", []) if p.get("status") == "PARSED"]
    check("存在已解析 PDF", bool(pdfs), f"parsed={len(pdfs)}")
    if not pdfs:
        raise SystemExit("无已解析 PDF,请先准备已解析 PDF 再运行")
    file_id = pdfs[0]["file_id"]
    r = c.request("GET", f"/pdfs/{file_id}", step="pdf-detail")
    check("GET /pdfs/{file_id} -> 200", r.status == 200, f"({r.status})")
    chapters = [ch for ch in (_body(r).get("chapters") or [])]
    check("PDF 章节非空", bool(chapters), f"chapters={len(chapters)}")
    if not chapters:
        raise SystemExit(f"PDF {file_id[:8]} 无章节,无法生成")
    sel = chapters[:2]

    # 3. 样卡预览(无副作用、豁免幂等键,故不幂等)
    r = c.request("POST", "/samples",
                  body={"file_id": file_id,
                        "chapter_ids": [ch["chapter_id"] for ch in sel],
                        "generation_config": _GEN_CONFIG},
                  step="samples")
    check("POST /samples -> 200", r.status == 200, f"({r.status})")
    cards = _sample_cards(_body(r))
    check("样卡数组非空", bool(cards), f"cards={len(cards)}")
    if not cards:
        raise SystemExit("样卡生成为空,无法继续")

    # 4. 创建牌组 + 生成任务(--skip-generate 时到此结束)
    if args.skip_generate:
        print("    [skip-generate] 跳过 POST /tasks 与评级,流程到样卡为止")
        return summary()

    try:
        deck_id = scope.create_deck("联调测试牌组")
    except RuntimeError as exc:
        raise SystemExit(f"创建牌组失败: {exc}") from None
    check("POST /decks 创建联调测试牌组 -> 201", True, f"deck={deck_id[:8]}")

    r = c.request("POST", "/tasks",
                  body={"file_id": file_id, "deck_id": deck_id,
                        "chapter_ids": [ch["chapter_id"] for ch in sel],
                        "generation_config": _GEN_CONFIG},
                  idempotent=True, step="task-create")
    check("POST /tasks -> 201", r.status == 201, f"({r.status})")
    task_id = _body(r).get("task_id")
    check("任务返回 task_id", isinstance(task_id, str), f"task={str(task_id)[:8]}")
    if not isinstance(task_id, str):
        raise SystemExit(f"任务创建响应缺 task_id: {_body(r)}")

    # 轮询至终态:间隔 5s,单次请求 60s 超时(client timeout),总上限 600s
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    status = ""
    while status not in _TERMINAL:
        r = c.request("GET", f"/tasks/{task_id}", step="task-poll")
        body = _body(r)
        status = str(body.get("status") or "")
        check("轮询 GET /tasks/{id} -> 200", r.status == 200,
              f"status={status} cards={body.get('generated_card_count')}")
        if r.status != 200:
            raise SystemExit(f"轮询任务 {task_id[:8]} 失败: HTTP {r.status}")
        if status in _TERMINAL:
            break
        if time.monotonic() >= deadline:
            raise SystemExit(f"任务 {task_id[:8]} 轮询超时(>{_POLL_TIMEOUT_S}s),最后状态 {status!r}")
        time.sleep(_POLL_INTERVAL_S)
    check("任务终态 COMPLETED", status == "COMPLETED", f"status={status}")
    if status != "COMPLETED":
        raise SystemExit(f"任务未完成: status={status} "
                         f"stage={body.get('failure_stage')} error_code={body.get('error_code')}")
    check("任务生成卡片数 > 0", bool(body.get("generated_card_count")),
          f"cards={body.get('generated_card_count')}")

    # 5. 牌组卡片 + 复习评级(任意状态可评级,C-06)
    r = c.request("GET", f"/decks/{deck_id}/cards", step="deck-cards")
    check("GET /decks/{id}/cards -> 200", r.status == 200, f"({r.status})")
    items = [it for it in _body(r).get("items", [])]
    check("牌组卡片非空", bool(items), f"cards={len(items)}")
    if not items:
        raise SystemExit(f"牌组 {deck_id[:8]} 无卡片,无法评级")
    card_id = items[0]["card_id"]
    r = c.request("POST", "/review-events",
                  body={"card_id": card_id, "rating": "GOOD",
                        "client_event_id": str(uuid.uuid4()),
                        "device_timezone": "Asia/Shanghai"},
                  idempotent=True, step="review-event")
    body = _body(r)
    check("POST /review-events -> 200", r.status == 200, f"({r.status})")
    check("评级返回 state/due", isinstance(body.get("state"), str) and bool(body.get("due")),
          f"state={body.get('state')} due={body.get('due')}")

    # 6. 看板(时区对齐,观察本周统计)
    r = c.request("GET", "/stats/dashboard?timezone=Asia/Shanghai", step="dashboard")
    body = _body(r)
    check("GET /stats/dashboard -> 200", r.status == 200, f"({r.status})")
    check("看板含周复习/掌握统计",
          "weekly_total" in body and "mastered_card_count" in body,
          f"weekly={body.get('weekly_total')} mastered={body.get('mastered_card_count')}")

    # 7. 清理(--keep 时保留牌组)
    if not args.keep:
        scope.cleanup()
        r = c.request("GET", f"/decks/{deck_id}", step="deck-after-cleanup")
        check("清理后牌组不可见(404)", r.status == 404, f"({r.status})")
    else:
        print("    [keep] 保留测试牌组,不清理")

    return summary()


if __name__ == "__main__":
    sys.exit(main())
