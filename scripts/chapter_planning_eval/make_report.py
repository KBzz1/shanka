"""chapter_planning_eval.make_report：零 API 重算章节规划质量报告。

只消费留档产物（``build_payloads.py`` 的 payload + ``run_live.py`` 的 replies，或
人工放置的同形 fixture）：每段回复跑**生产** ``validate_boundaries``（记录确定性
丢弃计数）→ 生产 ``merge_boundaries`` → ``metrics.evaluate`` → 打印报告并写
``report.md`` + ``metrics.json``。重复运行不产生任何模型调用。
用法：``conda run -n shanka-backend python scripts/chapter_planning_eval/make_report.py [--book NAME] [--tolerance 1]``
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "main"))

from app.config import Settings
from metrics import evaluate
from services.chapters.planner import merge_boundaries
from services.chapters.validator import (
    normalize_title,
    validate_boundaries,
)

OUT = Path(__file__).resolve().parent / "run"


def _dropped_reasons(
    raw: dict, kept_names: set[str], seg_start: int, seg_end: int
) -> Counter:
    """确定性丢弃计数重建（harness 观测专用；生产 planner 同逻辑只记日志）。

    与 validator 过滤顺序一致的近似归因：越界 / 同页 / 同名 / 段上限截断。
    """
    reasons: Counter = Counter()
    seen_pages: set[int] = set()
    seen_titles: set[str] = set()
    kept_count = 0
    for boundary in raw.get("chapters", []):
        page = int(boundary["start_page"])
        title = str(boundary["title"]).strip()
        norm = normalize_title(title)
        if not (seg_start <= page <= seg_end):
            reasons["out_of_segment"] += 1
        elif page in seen_pages:
            reasons["dup_page"] += 1
        elif not norm or norm in seen_titles:
            reasons["dup_title"] += 1
        elif kept_names and title not in kept_names and kept_count >= len(kept_names):
            reasons["capped"] += 1
        else:
            seen_pages.add(page)
            seen_titles.add(norm)
            kept_count += 1
    return reasons


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--book", default=None, help="书名（run/ 下目录名；缺省取唯一一个）"
    )
    parser.add_argument(
        "--tolerance", type=int, default=1, help="边界匹配页级容差（默认 ±1）"
    )
    args = parser.parse_args()

    book_dir = OUT / args.book if args.book else None
    if book_dir is None or not book_dir.exists():
        candidates = (
            sorted(p for p in OUT.iterdir() if p.is_dir()) if OUT.exists() else []
        )
        if len(candidates) == 1:
            book_dir = candidates[0]
        else:
            print(f"无法定位书目录；现有: {[p.name for p in candidates]}")
            return 1

    gt_chapters = _load_json(book_dir / "ground_truth.json")
    meta = _load_json(book_dir / "meta.json")
    pages = [
        json.loads(line)
        for line in (book_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    reply_files = sorted((book_dir / "replies").glob("seg_*.json"))
    if not reply_files:
        print(f"{book_dir / 'replies'} 无留档回复；先跑 run_live.py")
        return 1

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    boundaries: list[dict] = []
    kept_total = 0
    dropped_total: Counter[str] = Counter()
    parse_failures = 0
    for reply_file in reply_files:
        seg_index = int(reply_file.stem.split("_")[1])
        reply = _load_json(reply_file)
        if reply.get("http_status") != 200:
            parse_failures += 1
            continue
        try:
            raw = json.loads(reply["content"])
        except ValueError:
            parse_failures += 1
            continue
        # 段页区间从该段 user 信封还原（build_payloads 与 reply 一一同名对应）
        user = (book_dir / "segments" / f"seg_{seg_index}_user.txt").read_text(
            encoding="utf-8"
        )
        payload = json.loads(
            user.removeprefix("<CHAPTER_PLANNER_INPUT>").removesuffix(
                "</CHAPTER_PLANNER_INPUT>"
            )
        )
        seg_start = payload["segment"]["start_page"]
        seg_end = payload["segment"]["end_page"]
        kept = validate_boundaries(
            raw,
            segment_start=seg_start,
            segment_end=seg_end,
            max_boundaries=settings.ai_chapter_max_boundaries_per_segment,
        )
        boundaries.extend(kept)
        kept_total += len(kept)
        # 确定性丢弃计数：生产 planner 只记日志；此处按输入-存留差重建（harness 观测专用）
        dropped_total.update(
            _dropped_reasons(raw, {b["title"] for b in kept}, seg_start, seg_end)
        )

    from infra.db.models import TextChunk

    chunks = [
        TextChunk(
            chunk_id=f"p{p['page_number']}",
            material_id="eval",
            chunk_seq=p["page_number"],
            page_number=p["page_number"],
            char_count=len(p["content"]),
            content=p["content"],
            content_sha256="eval",
            created_at="eval",
        )
        for p in pages
    ]
    merged = merge_boundaries(
        [{"title": b["title"], "start_page": b["start_page"]} for b in boundaries],
        first_page=chunks[0].page_number,
        last_page=chunks[-1].page_number,
        material_name=book_dir.name,
    )

    report = evaluate(
        book=book_dir.name,
        total_pages=len(pages),
        gt_chapters=gt_chapters,
        predicted_boundaries=boundaries,
        merged_chapters=merged,
        tolerance=args.tolerance,
    )
    report.segment_kept = kept_total
    report.segment_dropped = dict(dropped_total)

    metrics_file = book_dir / "metrics.json"
    metrics_file.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        f"# 章节规划质量评测：{report.book}",
        "",
        (
            f"- 样本：{report.total_pages} 页；GT {report.gt_chapter_count} 章"
            f"（来源：{meta.get('ground_truth_source', 'outline')}）；"
            f"模型 {meta.get('model', '?')}；回复段 {len(reply_files)}（解析失败 {parse_failures}）"
        ),
        f"- 合并后章节 {len(merged)} 个"
        + (
            f"（首个「{merged[0]['name']}」{merged[0]['start_page']}..{merged[0]['end_page']} 为确定性补齐）"
            if merged and merged[0]["name"] == "开篇"
            else ""
        ),
        f"- 确定性校验：存留边界 {kept_total} 条；丢弃 {sum(dropped_total.values())} 条 {dict(dropped_total) or ''}",
        (
            f"- **边界 F1（±{args.tolerance} 页容差）**：precision={report.boundary.precision:.3f} "
            f"recall={report.boundary.recall:.3f} F1={report.boundary.f1:.3f} "
            f"（预测 {report.predicted_boundary_count} 个边界）"
        ),
        (
            f"- **章节区间 IoU**：均值 {report.iou_mean:.3f} / 中位数 {report.iou_median:.3f} / "
            f"≥0.5 覆盖率 {report.iou_coverage_half:.1%}"
        ),
        "- **标题相似度（匹配对 bigram Dice）**："
        + (
            f"{report.title_dice_mean:.3f}"
            if report.title_dice_mean is not None
            else "无匹配对"
        ),
        f"- **退化**：{'是（0 有效边界 → 整本单章）' if report.degraded else '否'}",
        "",
        "## 边界匹配明细（预测页, GT 页, 页距）",
        "",
    ]
    lines += [f"- {m[0]} ← {m[1]}（距 {m[2]}）" for m in report.boundary.matched] or [
        "- 无"
    ]
    lines += [
        "",
        "## 诚实限制",
        "",
        "- Ground truth 是书的 outline（页粒度）；章中起头/目录页码偏移由 ±容差吸收，残余误差计入模型；",
        "- 单书样本、单次运行，绝对值指示性（planning_ablation 同款限制）；重跑可复现（回复已留档）；",
        "- 开篇补齐为确定性产物，不计入预测边界（避免夸大 recall）。",
        "",
    ]
    report_file = book_dir / "report.md"
    report_file.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n已写 {report_file} 与 {metrics_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
