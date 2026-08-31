"""task_quality_report.py：单任务质量量化报告（docs/Architecture/generation-quality-metrics.md）。

只读评估工具：给定 task_id，输出 A 质量组 / B 编排组 / C 效率成本组指标与参考值对照，
用于密度制校准（V25-D-26）与 rubric 观测（V25-D-28）。零写入：全部查询走只读连接。

用法：
    conda run -n shanka-backend python scripts/task_quality_report.py --task-id <uuid>
    conda run -n shanka-backend python scripts/task_quality_report.py --task-id <uuid> --db ../main/data/shanka.db
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "main"))

from app.config import Settings
from services.generation.quota import (
    interval_for_chapter,
)

_SEP = "=" * 72
_SUB = "-" * 72

# 初始参考值（generation-quality-metrics.md；≥50 卡后校准修订）
A1_HEALTHY, A1_WATCH = 2.5, 2.0
A2_P50_TARGET, A2_P25_TARGET = 9, 7
A3_LOW_SCORE_TOTAL = 4
A3_LOW_RATIO_TARGET = 0.10
B1_LOW, B1_HIGH = 0.60, 1.20
B2_MAX_PP = 10
B4_DUP_TARGET = 0.05


def _quantile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return float(ordered[idx])


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _a_group(conn: sqlite3.Connection, task_id: str) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT target_difficulty, evidence_score, correctness_score,
               difficulty_score, learning_value_score, rubric_total_score
        FROM cards WHERE source_task_id = ? AND rubric_total_score IS NOT NULL
        """,
        (task_id,),
    ).fetchall()
    totals = [r[5] for r in rows]
    count = len(rows)
    dims = {
        "evidence": [r[1] for r in rows if r[1] is not None],
        "correctness": [r[2] for r in rows if r[2] is not None],
        "difficulty": [r[3] for r in rows if r[3] is not None],
        "learning_value": [r[4] for r in rows if r[4] is not None],
    }
    generated = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE source_task_id = ?", (task_id,)
    ).fetchone()[0]
    return {
        "rows": rows,
        "count": count,
        "generated": generated,
        "avg": {k: (sum(v) / len(v) if v else 0.0) for k, v in dims.items()},
        "p50": _quantile(totals, 0.5),
        "p25": _quantile(totals, 0.25),
        "low_ratio": (sum(1 for t in totals if t <= A3_LOW_SCORE_TOTAL) / count) if count else 0.0,
        "coverage": (count / generated) if generated else 0.0,
    }


def _b_group(conn: sqlite3.Connection, task_id: str, settings: Settings) -> dict[str, object]:
    config_row = conn.execute(
        "SELECT generation_config FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    config = json.loads(config_row[0]) if config_row and config_row[0] else {}
    mode = str(config.get("coverage_mode", config.get("quantity_tendency", "BALANCED")))
    ratio = config.get("difficulty_ratio", {})
    basic = ratio.get("basic", 0) / 100
    understanding = ratio.get("understanding", 0) / 100
    deep = ratio.get("deep_question", 0) / 100

    units = conn.execute(
        """
        SELECT target_difficulty, source_chunk_ids, chapter_id FROM knowledge_points
        WHERE task_id = ?
        """,
        (task_id,),
    ).fetchall()
    actual = {"BASIC": 0, "UNDERSTANDING": 0, "DEEP_QUESTION": 0}
    cited_chunks: set[str] = set()
    chapter_chunks: dict[str, set[str]] = {}
    for target, chunk_ids_json, chapter_id in units:
        if target in actual:
            actual[target] += 1
        try:
            ids = json.loads(chunk_ids_json or "[]")
        except (ValueError, TypeError):
            ids = []
        cited_chunks.update(ids)
        chapter_chunks.setdefault(chapter_id or "", set()).update(ids)

    snapshot_row = conn.execute(
        "SELECT selected_chapters, file_id FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    file_id = snapshot_row[1]
    try:
        selected = json.loads(snapshot_row[0] or "[]")
    except (ValueError, TypeError):
        selected = []
    selected_ids = [str(e.get("chapter_id")) for e in selected if isinstance(e, dict)]
    qmarks = ",".join("?" for _ in selected_ids) or "''"
    chapter_totals = conn.execute(
        f"""
        SELECT c.chapter_id, COUNT(tc.chunk_id) FROM chapters c
        LEFT JOIN text_chunks tc ON tc.file_id = c.file_id
            AND tc.page_number BETWEEN c.start_page AND c.end_page
        WHERE c.file_id = ? AND c.chapter_id IN ({qmarks})
        GROUP BY c.chapter_id
        """,
        (file_id, *selected_ids),
    ).fetchall()
    total_chunks = sum(c for _, c in chapter_totals)

    # 期望区间（镜像 planning_executor：按章节字符重建目标区间上界合计）
    expected_max = 0
    for chapter_id, chunk_count in chapter_totals:
        chars_row = conn.execute(
            """
            SELECT COALESCE(SUM(tc.char_count), 0) FROM chapters c
            LEFT JOIN text_chunks tc ON tc.file_id = c.file_id
                AND tc.page_number BETWEEN c.start_page AND c.end_page
            WHERE c.chapter_id = ?
            """,
            (chapter_id,),
        ).fetchone()
        chars = int(chars_row[0]) if chars_row else 0
        anchors = {
            "COMPACT": settings.cards_per_10k_compact,
            "BALANCED": settings.cards_per_10k_balanced,
            "EXTENSIVE": settings.cards_per_10k_extensive,
        }
        expected_max += interval_for_chapter(chars, mode, anchors)[1]
    expected_share = {
        "BASIC": expected_max * basic,
        "UNDERSTANDING": expected_max * understanding,
        "DEEP_QUESTION": expected_max * deep,
    }
    utilization = {
        d: (actual[d] / expected_share[d]) if expected_share[d] else 0.0
        for d in actual
    }
    deviation = {
        d: (
            (actual[d] / sum(actual.values()) * 100 - d_ratio * 100)
            if sum(actual.values())
            else 0.0
        )
        for d, d_ratio in (
            ("BASIC", basic),
            ("UNDERSTANDING", understanding),
            ("DEEP_QUESTION", deep),
        )
    }
    dup = conn.execute(
        """
        SELECT COALESCE(AVG(b.duplicate_rate), 0) FROM batches b WHERE b.task_id = ?
        """,
        (task_id,),
    ).fetchone()[0]
    return {
        "mode": mode,
        "actual": actual,
        "expected_max": expected_max,
        "utilization": utilization,
        "deviation": deviation,
        "content_coverage": (len(cited_chunks) / total_chunks) if total_chunks else 0.0,
        "total_chunks": total_chunks,
        "duplicate_avg": float(dup),
    }


def _c_group(conn: sqlite3.Connection, task_id: str) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT stage, attempt_no, status, duration_ms, cache_hit, cache_miss, output_tokens
        FROM llm_call_attempts WHERE task_id = ?
        """,
        (task_id,),
    ).fetchall()
    total = len(rows)
    retries = sum(1 for r in rows if (r[1] or 1) > 1)
    gen = sum(1 for r in rows if r[0] == "GENERATING" and r[2] == "SUCCESS")
    scoring = sum(1 for r in rows if r[0] == "SCORING" and r[2] == "SUCCESS")
    generated_cards = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE source_task_id = ?", (task_id,)
    ).fetchone()[0]
    wall = conn.execute(
        """
        SELECT CAST((julianday(ended_at) - julianday(created_at)) * 86400000 AS INTEGER)
        FROM generation_operations
        WHERE task_id = ? AND ended_at IS NOT NULL ORDER BY created_at DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    tokens = {
        "cache_hit": sum(r[4] or 0 for r in rows),
        "cache_miss": sum(r[5] or 0 for r in rows),
        "output": sum(r[6] or 0 for r in rows),
    }
    return {
        "total_calls": total,
        "retries": retries,
        "retry_ratio": (retries / total) if total else 0.0,
        "calls_per_card": ((gen + scoring) / generated_cards) if generated_cards else 0.0,
        "wall_ms": wall[0] if wall else None,
        "wall_per_card_ms": (wall[0] / generated_cards) if wall and generated_cards else None,
        "tokens": tokens,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="单任务质量量化报告（只读）")
    parser.add_argument("--task-id", required=True)
    parser.add_argument(
        "--db", default=str(REPO_ROOT / "main" / "data" / "shanka.db"), help="SQLite 路径"
    )
    args = parser.parse_args()

    settings = Settings()
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)

    task = conn.execute(
        "SELECT status, stage, error_code, skipped_planning_group_count FROM tasks WHERE task_id = ?",
        (args.task_id,),
    ).fetchone()
    if task is None:
        print(f"任务不存在：{args.task_id}")
        sys.exit(1)
    print(_SEP)
    print(f"任务质量报告  task={args.task_id[:8]}  status={task[0]}  stage={task[1]}  "
          f"error={task[2] or '-'}  skipped_groups={task[3]}")

    a = _a_group(conn, args.task_id)
    print(_SEP)
    print("A 质量组（rubric 纯观测，V25-D-28：不设门禁）")
    print(_SUB)
    if a["count"] == 0:
        print("  无已评分卡片（A4 评分覆盖率 = 0）")
    else:
        for dim, avg in a["avg"].items():
            flag = "✓" if avg >= A1_HEALTHY else ("!" if avg >= A1_WATCH else "✗")
            print(f"  A1 {dim:16s} 均分 {avg:.2f} / 3   {flag}")
        print(f"  A2 总分 P50 {_fmt(a['p50'], 0)}（目标 ≥{A2_P50_TARGET}）  "
              f"P25 {_fmt(a['p25'], 0)}（目标 ≥{A2_P25_TARGET}）")
        flag = "✓" if a["low_ratio"] < A3_LOW_RATIO_TARGET else "✗"
        print(f"  A3 低分占比（≤{A3_LOW_SCORE_TOTAL}）{a['low_ratio'] * 100:.1f}%   {flag}")
        print(f"  A4 评分覆盖率 {a['coverage'] * 100:.1f}%（{a['count']}/{a['generated']}）")

    b = _b_group(conn, args.task_id, settings)
    print(_SEP)
    print(f"B 编排组（coverage_mode={b['mode']}）")
    print(_SUB)
    ratio_map = {"BASIC": 0.4, "UNDERSTANDING": 0.4, "DEEP_QUESTION": 0.2}
    for d in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION"):
        util = b["utilization"][d]
        flag = "✓" if B1_LOW <= util <= B1_HIGH else "!"
        expected_d = b["expected_max"] * ratio_map[d]
        print(f"  B1 {d:16s} 单元 {b['actual'][d]:3d} / 期望上界 {expected_d:.1f}"
              f"  利用率 {util * 100:5.1f}%  {flag}")
        flag2 = "✓" if abs(b["deviation"][d]) <= B2_MAX_PP else "✗"
        print(f"  B2 {d:16s} 难度偏差 {b['deviation'][d]:+.1f}pp（|≤{B2_MAX_PP}|） {flag2}")
    print(f"  B3 内容覆盖率 {b['content_coverage'] * 100:.1f}%（引用 {b['total_chunks'] and round(b['content_coverage'] * b['total_chunks'])}/{b['total_chunks']} chunks）")
    print(f"  B4 重复率均值 {b['duplicate_avg'] * 100:.1f}%")

    c = _c_group(conn, args.task_id)
    print(_SEP)
    print("C 效率/成本组")
    print(_SUB)
    print(f"  C1 每卡调用数 {c['calls_per_card']:.2f}（生成+评分 / 卡）")
    if c["wall_per_card_ms"] is not None:
        print(f"  C2 每卡墙钟 {c['wall_per_card_ms'] / 1000:.1f}s（任务 {c['wall_ms'] / 1000:.1f}s）")
    print(f"  C3 重试率 {c['retry_ratio'] * 100:.1f}%（{c['retries']}/{c['total_calls']} 次尝试）")
    print(f"  tokens: cache_hit={c['tokens']['cache_hit']}  cache_miss={c['tokens']['cache_miss']}  output={c['tokens']['output']}")
    print(_SEP)
    print("参考值与校准纪律见 docs/Architecture/generation-quality-metrics.md")
    conn.close()


if __name__ == "__main__":
    main()
