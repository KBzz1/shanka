"""chapter_planning_eval.metrics：章节边界规划质量指标（V25-D-36 离线评测）。

Ground truth = 书的自带目录（parse_pdf 的 outline 章节）；预测 = AI 边界经生产
validate_boundaries 校验后 merge_boundaries 合并的章节。指标口径：

- **边界 F1（主指标）**：预测边界集（各段存留的 start_page，不含自动补的「开篇」
  标记——它是确定性产物而非模型输出）vs 目录边界集；一对一贪心匹配（按页距升序），
  页级容差 ±tol（吸收 outline 混合页/章节未起首行的固有页粒度误差）。
- **章节区间 IoU**：合并后章节（含开篇）vs 目录章节；每个 GT 章取与其重叠最大页数
  的预测章，IoU = 交集/并集；报均值/中位数/≥0.5 覆盖率。
- **标题相似度**：已匹配（边界 F1 意义上）的对，字符 bigram Dice（CJK 友好、
  长度 1 时退化 unigram）；无匹配对不计入。
- **退化**：预测 0 边界（merge 产出整本单章）→ 退化标记，边界 recall 记 0。

纯函数、零 I/O；``--selftest`` 跑内置手算用例（脚本区不进 pytest，同 ablation 惯例）。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field


@dataclass
class BoundaryMetrics:
    precision: float
    recall: float
    f1: float
    matched: list[tuple[int, int, int]]  # (pred_page, gt_page, page_distance)
    tolerance: int


@dataclass
class EvalReport:
    book: str
    total_pages: int
    gt_chapter_count: int
    predicted_boundary_count: int
    degraded: bool
    boundary: BoundaryMetrics
    iou_mean: float
    iou_median: float
    iou_coverage_half: float
    title_dice_mean: float | None
    segment_dropped: dict[str, int] = field(default_factory=dict)
    segment_kept: int = 0

    def to_dict(self) -> dict:
        return {
            "book": self.book,
            "total_pages": self.total_pages,
            "gt_chapter_count": self.gt_chapter_count,
            "predicted_boundary_count": self.predicted_boundary_count,
            "degraded": self.degraded,
            "boundary_precision": self.boundary.precision,
            "boundary_recall": self.boundary.recall,
            "boundary_f1": self.boundary.f1,
            "boundary_tolerance_pages": self.boundary.tolerance,
            "boundary_matches": self.boundary.matched,
            "iou_mean": self.iou_mean,
            "iou_median": self.iou_median,
            "iou_coverage_half": self.iou_coverage_half,
            "title_dice_mean": self.title_dice_mean,
            "segment_kept": self.segment_kept,
            "segment_dropped": self.segment_dropped,
        }


def _greedy_match(
    pred_pages: list[int], gt_pages: list[int], tol: int
) -> list[tuple[int, int, int]]:
    """一对一贪心匹配：全部 (pred, gt) 组合按页距升序（并列按页码序）取互不相交对。"""
    candidates = sorted(
        (abs(p - g), p, g) for p in pred_pages for g in gt_pages if abs(p - g) <= tol
    )
    used_pred: set[int] = set()
    used_gt: set[int] = set()
    matched: list[tuple[int, int, int]] = []
    for dist, p, g in candidates:
        if p in used_pred or g in used_gt:
            continue
        used_pred.add(p)
        used_gt.add(g)
        matched.append((p, g, dist))
    return matched


def boundary_f1(
    pred_pages: list[int], gt_pages: list[int], *, tolerance: int = 1
) -> BoundaryMetrics:
    """边界 precision/recall/F1：容差页级匹配（±tol 一对一贪心）。"""
    if not pred_pages and not gt_pages:
        return BoundaryMetrics(1.0, 1.0, 1.0, [], tolerance)
    matched = _greedy_match(pred_pages, gt_pages, tolerance)
    precision = len(matched) / len(pred_pages) if pred_pages else 0.0
    recall = len(matched) / len(gt_pages) if gt_pages else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return BoundaryMetrics(precision, recall, f1, matched, tolerance)


def _interval_iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0]) + 1
    if inter <= 0:
        return 0.0
    union = (a[1] - a[0] + 1) + (b[1] - b[0] + 1) - inter
    return inter / union


def chapter_iou(
    pred: list[tuple[int, int]], gt: list[tuple[int, int]]
) -> tuple[list[float], float, float, float]:
    """区间 IoU：每个 GT 章取重叠页数最大的预测章；返回 (逐 GT IoU, 均值, 中位数, ≥0.5 覆盖率)。"""
    if not gt:
        return [], 0.0, 0.0, 0.0
    scores = [max((_interval_iou(g, p) for p in pred), default=0.0) for g in gt]
    ordered = sorted(scores)
    mid = len(ordered) // 2
    median = (
        ordered[mid] if len(ordered) % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2
    )
    mean = sum(scores) / len(scores)
    coverage = sum(1 for s in scores if s >= 0.5) / len(scores)
    return scores, mean, median, coverage


def title_dice(a: str, b: str) -> float:
    """字符 bigram Dice（CJK 友好）；单字符串退化 unigram。"""
    import re

    normalize = lambda s: re.sub(
        r"[\s，。、；：？！,.;:?!()（）【】\[\]《》<>\"'‘’“”·…\-—_/\\|]+", "", s
    ).lower()
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ga = [na[i : i + 2] or na[i] for i in range(len(na))] if len(na) > 1 else [na]
    gb = [nb[i : i + 2] or nb[i] for i in range(len(nb))] if len(nb) > 1 else [nb]
    from collections import Counter

    ca, cb = Counter(ga), Counter(gb)
    overlap = sum((ca & cb).values())
    if overlap == 0:
        return 0.0
    return 2 * overlap / (len(ga) + len(gb))


def matched_title_dice(
    pred_titles: dict[int, str],
    gt_titles: dict[int, str],
    matched: list[tuple[int, int, int]],
) -> float | None:
    """边界匹配对上的标题 Dice 均值；无匹配对返回 None（不计入）。"""
    scores = [
        title_dice(pred_titles[p], gt_titles[g])
        for p, g, _ in matched
        if p in pred_titles and g in gt_titles
    ]
    if not scores:
        return None
    return sum(scores) / len(scores)


def evaluate(
    *,
    book: str,
    total_pages: int,
    gt_chapters: list[dict],
    predicted_boundaries: list[dict],
    merged_chapters: list[dict],
    tolerance: int = 1,
) -> EvalReport:
    """汇总入口：GT 章节字典 / 预测边界字典（validate 后）/ 合并章节字典（merge 产物）。"""
    gt_pages = [int(c["start_page"]) for c in gt_chapters]
    # 预测边界集不含「开篇」（确定性补齐标记，非模型输出）
    predicted = [b for b in predicted_boundaries if b["title"] != "开篇"]
    pred_pages = [int(b["start_page"]) for b in predicted]
    degraded = len(predicted) == 0
    if degraded:  # 整本退化：边界 recall 以 0 计（模型没有识别出任何结构）
        boundary = BoundaryMetrics(0.0, 0.0, 0.0, [], tolerance)
    else:
        boundary = boundary_f1(pred_pages, gt_pages, tolerance=tolerance)
    pred_intervals = [
        (int(c["start_page"]), int(c["end_page"])) for c in merged_chapters
    ]
    gt_intervals = [(int(c["start_page"]), int(c["end_page"])) for c in gt_chapters]
    _scores, iou_mean, iou_median, iou_cov = chapter_iou(pred_intervals, gt_intervals)
    dice = matched_title_dice(
        {int(b["start_page"]): str(b["title"]) for b in predicted},
        {int(c["start_page"]): str(c["name"]) for c in gt_chapters},
        boundary.matched,
    )
    return EvalReport(
        book=book,
        total_pages=total_pages,
        gt_chapter_count=len(gt_chapters),
        predicted_boundary_count=len(predicted),
        degraded=degraded,
        boundary=boundary,
        iou_mean=iou_mean,
        iou_median=iou_median,
        iou_coverage_half=iou_cov,
        title_dice_mean=dice,
    )


def _selftest() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    # 边界 F1：全对
    m = boundary_f1([9, 45, 120], [9, 45, 120])
    check("exact f1=1", m.f1 == 1.0 and m.precision == 1.0 and m.recall == 1.0)
    # ±1 容差：pred 46 匹配 gt 45
    m = boundary_f1([9, 46], [9, 45, 120], tolerance=1)
    check(
        "tolerance match", m.matched == [(9, 9, 0), (46, 45, 1)] and m.recall == 2 / 3
    )
    # 容差外不算
    m = boundary_f1([47], [45], tolerance=1)
    check("outside tolerance", m.f1 == 0.0)
    # 一对一：两个 pred 挤同一个 gt 只留最近
    m = boundary_f1([44, 45], [45], tolerance=1)
    check("one-to-one keeps nearest", len(m.matched) == 1 and m.matched[0][0] == 45)
    # 双空 = 完美；预测空 = 全 0
    check("both empty", boundary_f1([], []).f1 == 1.0)
    check("pred empty zero", boundary_f1([], [9]).f1 == 0.0)
    # 区间 IoU
    check("iou same", _interval_iou((1, 10), (1, 10)) == 1.0)
    check("iou half", abs(_interval_iou((1, 10), (6, 15)) - 5 / 15) < 1e-9)
    check("iou disjoint", _interval_iou((1, 5), (6, 10)) == 0.0)
    scores, mean, median, cov = chapter_iou(
        [(1, 10), (11, 20)], [(1, 10), (11, 20), (21, 30)]
    )
    check(
        "iou per-gt",
        scores == [1.0, 1.0, 0.0]
        and abs(mean - 2 / 3) < 1e-9
        and abs(median - 1.0) < 1e-9
        and cov == 2 / 3,
    )
    # 标题 Dice
    check("dice identical", title_dice("第 3 章 记忆系统", "第 3 章 记忆系统") == 1.0)
    check(
        "dice punctuation-insensitive",
        title_dice("第3章记忆系统", "第 3 章 记忆系统") == 1.0,
    )
    check("dice disjoint", title_dice("绪论", "附录") == 0.0)
    check("dice partial", 0.0 < title_dice("记忆系统", "记忆管理") < 1.0)
    # evaluate 端到端：GT 3 章，预测 2 章 + 开篇自动补齐
    gt = [
        {"name": "第 1 章 绪论", "start_page": 1, "end_page": 8},
        {"name": "第 2 章 记忆", "start_page": 9, "end_page": 50},
        {"name": "第 3 章 检索", "start_page": 51, "end_page": 100},
    ]
    report = evaluate(
        book="t",
        total_pages=100,
        gt_chapters=gt,
        predicted_boundaries=[{"title": "第 2 章 记忆", "start_page": 9}],
        merged_chapters=[
            {"name": "开篇", "start_page": 1, "end_page": 8},
            {"name": "第 2 章 记忆", "start_page": 9, "end_page": 100},
        ],
    )
    check("e2e no degrade", not report.degraded)
    check(
        "e2e f1", abs(report.boundary.f1 - 0.5) < 1e-9
    )  # precision=1.0, recall=1/3 → F1=0.5
    check("e2e dice", report.title_dice_mean == 1.0)
    # 退化：0 预测边界
    report = evaluate(
        book="t",
        total_pages=100,
        gt_chapters=gt,
        predicted_boundaries=[],
        merged_chapters=[{"name": "整本", "start_page": 1, "end_page": 100}],
    )
    check("e2e degraded flagged", report.degraded and report.boundary.recall == 0.0)
    # json 可序列化
    json.dumps(report.to_dict(), ensure_ascii=False)

    if failures:
        print("SELFTEST FAIL:", failures)
        return 1
    print("selftest ok: boundary F1 / IoU / title Dice / degradation / json")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
