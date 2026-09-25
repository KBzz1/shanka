"""make_report.py：问答直通验收聚合报告与榜单（零 API，可反复重算）。

输入：run/<id>/{cards.json, meta.json, judges/A/scores.json, judges/B/scores.json}
      + fixtures/ground_truth.json。

确定性指标（脚本本地计算，复用生产 normalize_question 同一把尺）：
  R1 召回率  = 被卡片命中的唯一 GT 项 / GT 唯一项数（重复题按 duplicate_of 归并）
  R2 自创率  = 无 GT 对应的卡 / 总卡数
  R3 重复卡  = 同一 GT 被多张卡命中
  R4 判断题分派正确率 = GT 判断题被正确形态（TRUE_FALSE + answer_boolean 一致）命中占比
裁判指标：Q1~Q4 均分（双裁判）、裁判间一致性（完全一致 / ±1 / 均值差）。
映射口径：卡片 front 与 GT 题干先做生产归一化精确匹配；不中再退宽匹配（去空白与句末
标点）；仍不中采用双裁判 gt_id 共识；冲突全部显式列出人工复核。

产出：run/<id>/report.md；并在 workspace/leaderboard.md 追加（或更新）该 run 行。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
REPO_MAIN = WORKSPACE.parents[1] / "main"
sys.path.insert(0, str(REPO_MAIN))

from services.generation.qa_planner_validator import normalize_question

_TERMINAL_PUNCT = "。．.!！?？，,、;；:："


def _lenient(text: str) -> str:
    stripped = normalize_question(text)
    stripped = stripped.strip().strip("\"“”‘’'「」")
    while stripped and stripped[-1] in _TERMINAL_PUNCT:
        stripped = stripped[:-1]
    return re.sub(r"\s+", "", stripped)


def _card_stem(card: dict) -> str:
    return card.get("front", "")


def _gt_stem(item: dict) -> str:
    return item.get("question") or item.get("statement") or ""


def build_mapping(cards: list[dict], gt_items: list[dict], judges: list[dict]) -> dict:
    canonical: dict[str, str] = {}
    for item in gt_items:
        cid = item.get("duplicate_of") or item["id"]
        canonical[item["id"]] = cid
    gt_by_norm: dict[str, str] = {}
    gt_by_lenient: dict[str, str] = {}
    for item in gt_items:
        cid = canonical[item["id"]]
        gt_by_norm.setdefault(normalize_question(_gt_stem(item)), cid)
        gt_by_lenient.setdefault(_lenient(_gt_stem(item)), cid)

    judge_votes: dict[str, list[str]] = {
        f"card-{i:02d}": [] for i in range(1, len(cards) + 1)
    }
    for judge in judges:
        for row in judge["cards"]:
            if row.get("gt_id") and row["gt_id"] != "NONE":
                judge_votes.setdefault(row["card_id"], []).append(
                    canonical.get(row["gt_id"], row["gt_id"])
                )

    mapping: dict[str, str | None] = {}
    method: dict[str, str] = {}
    for i, card in enumerate(cards, start=1):
        cid = f"card-{i:02d}"
        exact = gt_by_norm.get(normalize_question(_card_stem(card)))
        if exact:
            mapping[cid], method[cid] = exact, "norm-exact"
            continue
        loose = gt_by_lenient.get(_lenient(_card_stem(card)))
        if loose:
            mapping[cid], method[cid] = loose, "lenient"
            continue
        votes = judge_votes.get(cid, [])
        if votes and len(set(votes)) == 1 and votes.count(votes[0]) == len(judges):
            mapping[cid], method[cid] = votes[0], "judge-consensus"
        else:
            mapping[cid], method[cid] = None, "unresolved"
    return mapping, method


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    args = parser.parse_args()

    run_dir = WORKSPACE / "run" / args.run
    cards = json.loads((run_dir / "cards.json").read_text(encoding="utf-8"))
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    gt = json.loads(
        (WORKSPACE / "fixtures" / "ground_truth.json").read_text(encoding="utf-8")
    )
    gt_items = gt["items"]
    judges = []
    for label in ("A", "B"):
        path = run_dir / "judges" / label / "scores.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["cards"]) == len(cards), (
            f"裁判 {label} 卡数不符：{len(data['cards'])}"
        )
        judges.append(data)

    mapping, method = build_mapping(cards, gt_items, judges)

    # ---- 确定性指标 ----
    unique_gt = {it.get("duplicate_of") or it["id"] for it in gt_items}
    matched_gt: dict[str, list[str]] = {}
    for i in range(1, len(cards) + 1):
        cid = f"card-{i:02d}"
        if mapping[cid]:
            matched_gt.setdefault(mapping[cid], []).append(cid)
    recall = len(matched_gt) / len(unique_gt)
    fabrication = sum(1 for v in mapping.values() if v is None) / len(cards)
    dup_pairs = {g: cs for g, cs in matched_gt.items() if len(cs) > 1}
    tf_gt = [it for it in gt_items if it["card_type"] == "TRUE_FALSE"]
    tf_ok, tf_bad = [], []
    for it in tf_gt:
        cid = it.get("duplicate_of") or it["id"]
        hits = matched_gt.get(cid, [])
        ok = any(
            cards[int(h[5:]) - 1].get("card_type") == "TRUE_FALSE"
            and cards[int(h[5:]) - 1].get("answer_boolean") == it["answer_boolean"]
            for h in hits
        )
        (tf_ok if ok else tf_bad).append(cid)
    tf_rate = len(tf_ok) / len(tf_gt)
    rubric_scored = sum(1 for c in cards if c.get("rubric_total_score") is not None)

    # ---- 裁判指标 ----
    dims = ("Q1", "Q2", "Q3", "Q4")
    means = {}
    for label, judge in zip(("A", "B"), judges):
        means[label] = {
            d: sum(r[d] for r in judge["cards"]) / len(judge["cards"]) for d in dims
        }
    agree = {}
    for d in dims:
        exact = sum(
            1 for a, b in zip(judges[0]["cards"], judges[1]["cards"]) if a[d] == b[d]
        )
        within1 = sum(
            1
            for a, b in zip(judges[0]["cards"], judges[1]["cards"])
            if abs(a[d] - b[d]) <= 1
        )
        diff = means["A"][d] - means["B"][d]
        agree[d] = {"exact": exact, "within1": within1, "mean_diff": round(diff, 2)}
    low_cards = [
        (r["card_id"], mapping.get(r["card_id"]), r["Q1"], r["Q2"], r["Q3"], r["Q4"])
        for r in judges[0]["cards"]
        if (r["Q1"] + r["Q2"] + r["Q3"] + r["Q4"]) <= 8
    ]

    gates = [
        (
            "G1 任务 COMPLETED 且生产评分覆盖 100%",
            meta.get("generated_card_count") is not None
            and rubric_scored == len(cards),
        ),
        ("G2 题库召回率 ≥ 90%", recall >= 0.9),
        ("G3 自创卡 = 0", fabrication == 0.0),
        ("G4 重复卡 = 0（植入重复题合并为 1 卡）", not dup_pairs),
        ("G5 判断题分派正确率 100%", tf_rate == 1.0),
    ]

    # ---- 报告 ----
    lines: list[str] = []
    lines.append(f"# QA 直通验收报告 · {args.run}")
    lines.append("")
    lines.append(
        f"- 任务 `{meta['task_id']}`（{len(meta['chapters'])} 章："
        + "、".join(c["name"] for c in meta["chapters"])
        + f"），全程 {meta['elapsed_seconds']}s"
    )
    lines.append(
        f"- 生成 {len(cards)} 卡 / GT 唯一项 {len(unique_gt)}；卡型分布 {meta['card_type_distribution']}"
    )
    lines.append(f"- 生产评分覆盖 {rubric_scored}/{len(cards)}")
    lines.append("")
    lines.append("## 客观门禁（QA-A，对标 B5 只含客观可判定项）")
    lines.append("")
    lines.append("| 门禁 | 结果 |")
    lines.append("| --- | --- |")
    for name, ok in gates:
        lines.append(f"| {name} | {'✅' if ok else '❌'} |")
    lines.append("")
    lines.append("## 确定性指标（R 组，题库级）")
    lines.append("")
    lines.append(f"- R1 召回率：**{len(matched_gt)}/{len(unique_gt)} = {recall:.1%}**")
    miss = sorted(unique_gt - set(matched_gt))
    if miss:
        lines.append(f"  - 未命中 GT：{', '.join(miss)}")
    lines.append(f"- R2 自创率：{fabrication:.1%}（无 GT 对应的卡）")
    lines.append(
        f"- R3 重复卡：{len(dup_pairs)} 组" + (f" → {dup_pairs}" if dup_pairs else "")
    )
    lines.append(
        f"- R4 判断题分派正确率：{len(tf_ok)}/{len(tf_gt)} = {tf_rate:.0%}"
        + (f"（异常：{tf_bad}）" if tf_bad else "")
    )
    unresolved = [c for c, m in mapping.items() if m is None]
    if unresolved:
        lines.append(f"- ⚠️ 映射未定卡（人工复核）：{unresolved}")
    lines.append("")
    lines.append("## 双裁判盲评（Q 组，rubric v1）")
    lines.append("")
    lines.append("| 维度 | 裁判 A | 裁判 B | 完全一致 | ±1 | 均值差 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for d in dims:
        a = agree[d]
        lines.append(
            f"| {d} | {means['A'][d]:.2f} | {means['B'][d]:.2f} | "
            f"{a['exact']}/{len(cards)} | {a['within1']}/{len(cards)} | {a['mean_diff']:+.2f} |"
        )
    if low_cards:
        lines.append("")
        lines.append("低分卡（裁判 A 总分 ≤8，人工抽检优先）：")
        for cid, g, *scores in low_cards:
            lines.append(f"- {cid} → {g or 'NONE'}：{scores}")
    lines.append("")
    lines.append("## 映射方法分布")
    lines.append("")
    from collections import Counter

    lines.append(f"- {dict(Counter(method.values()))}")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 榜单 ----
    board = WORKSPACE / "leaderboard.md"
    if not board.exists():
        board.write_text(
            "# QA 直通模式（QA_DIRECT）评测榜单\n\n"
            "评分维度与门禁定义见 [rubric.md](rubric.md)；方法论 = 既有「外部双裁判盲评协议」\n"
            "（generation-quality-metrics.md）+ B5 式生产 HTTP 链真实验收。参考值初始为经验设定，\n"
            "不具备统计效力，按质量指标校准纪律（累计 ≥50 卡后复核修订并留痕）。\n\n"
            "| run | 任务 | 卡数/GT | 召回 | 自创 | 重复 | 判断题 | Q1 | Q2 | Q3 | Q4 | 门禁 |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n",
            encoding="utf-8",
        )
    failed_gates = [n.split(" ", 1)[0] for n, ok in gates if not ok]
    gate_str = (
        "全过" if not failed_gates else "未全过（" + "、".join(failed_gates) + "）"
    )
    row = (
        f"| {args.run} | `{meta['task_id'][:8]}` | {len(cards)}/{len(unique_gt)} "
        f"| {recall:.0%} | {fabrication:.0%} | {len(dup_pairs)} | {tf_rate:.0%} "
        f"| {means['A']['Q1']:.2f}/{means['B']['Q1']:.2f} "
        f"| {means['A']['Q2']:.2f}/{means['B']['Q2']:.2f} "
        f"| {means['A']['Q3']:.2f}/{means['B']['Q3']:.2f} "
        f"| {means['A']['Q4']:.2f}/{means['B']['Q4']:.2f} | {gate_str} |"
    )
    text = board.read_text(encoding="utf-8")
    pattern = re.compile(rf"^\| {re.escape(args.run)} \|.*\|$", re.MULTILINE)
    text = (
        pattern.sub(row, text)
        if pattern.search(text)
        else text.rstrip("\n") + "\n" + row + "\n"
    )
    board.write_text(text, encoding="utf-8")

    print(f"OK：{run_dir / 'report.md'}")
    print(f"榜单已更新：{board}")
    gate_detail = "；".join(
        f"{n.split(' ', 1)[0]}={'过' if ok else '未过'}" for n, ok in gates
    )
    print(f"门禁：{gate_detail}")
    print(
        f"召回 {recall:.0%} / 自创 {fabrication:.0%} / 重复 {len(dup_pairs)} / 判断题 {tf_rate:.0%}"
    )


if __name__ == "__main__":
    main()
