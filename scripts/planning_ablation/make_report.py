"""planning_ablation.make_report：汇总全部 cell 指标与评委分数，产出 report.md。"""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUN = BASE / "run"
CELLS = [
    "ch1-COMPACT-base",
    "ch1-COMPACT-assess",
    "ch1-BALANCED-base",
    "ch1-BALANCED-assess",
    "ch1-EXTENSIVE-base",
    "ch1-EXTENSIVE-assess",
    "ch10-EXTENSIVE-base",
    "ch10-EXTENSIVE-assess",
]
OLD_BASELINE = {"ch1-COMPACT": 19, "ch1-BALANCED": None, "ch1-EXTENSIVE": 19}


def avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def card_scores(cell_dir: Path) -> dict:
    data = json.loads((cell_dir / "judge_cards" / "scores.json").read_text(encoding="utf-8"))
    dims = ("evidence", "correctness", "difficulty", "learning_value")
    out = {}
    for dim in dims:
        out[dim] = avg([c["scores"][dim] for c in data["cards"]])
    out["_n"] = len(data["cards"])
    out["_notes"] = [c.get("note", "") for c in data["cards"] if c["scores"]["difficulty"] < 3][:3]
    return out


def planning_scores(cell_dir: Path) -> dict:
    data = json.loads(
        (cell_dir / "judge_planning" / "scores.json").read_text(encoding="utf-8")
    )
    out = {k: data[k] for k in ("coverage", "atomicity", "tier", "units")}
    out["_notes"] = data.get("notes", {})
    return out


def main() -> None:
    coarse = json.loads((RUN / "coarse_summary.json").read_text(encoding="utf-8"))
    fine = json.loads((RUN / "fine_summary.json").read_text(encoding="utf-8"))
    rows = {}
    for cell in CELLS:
        cell_dir = RUN / cell
        c, f = coarse[cell], fine[cell]
        card_check = json.loads((cell_dir / "card_check.json").read_text(encoding="utf-8"))
        rows[cell] = {
            "coarse": c,
            "fine": f,
            "card_check": card_check,
            "planning": planning_scores(cell_dir),
            "card": card_scores(cell_dir),
        }

    lines = []
    lines.append("# 两阶段规划消融实验报告（子代理驱动，零 API）")
    lines.append("")
    lines.append("- 日期：2026-09-09；被测模型 = ZCode 子代理（≠ 生产 deepseek-v4-flash，绝对数值仅指示性，结论看相对差异）")
    lines.append("- 章节：第 1 章（21 块 / 33,518 字）+ 第 10 章（34 块 / 58,895 字）；难度比例统一 40/40/20；密度锚点 6/12/20 每万字")
    lines.append("- 链路：粗规划（整章一次）→ 确定性校验（tier 过滤/上限截断/标题去重）→ 精规划（抽样 ≤20 主题一批）→ 单元校验（topic 契约/来源/难度区间截断/tier 注入）→ 卡片生成（抽样 10 单元）→ rubric v3 卡评 + 规划四维评委")
    lines.append("- 与生产的保真度差异：精规划生产 8 主题/批 + 20k 字符上限；卡生成生产 1 单元 1 调用。实验放宽（子代理无 token 预算），结构语义一致")
    lines.append("- 评委为同源模型，存在自评偏宽风险（尤其卡评四维普遍接近满分）；规划评委相对严格")
    lines.append("")
    lines.append("## 一、确定性指标（代码计算，非模型判断）")
    lines.append("")
    lines.append("| cell | 原始主题 | 终主题 | 目标区间 | 命中 | tier 分布 (C/I/L) | B3 覆盖 | tier 违规 | 重复标题 | 近重复对 | 外推单元/上限 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for cell in CELLS:
        c, f = rows[cell]["coarse"], rows[cell]["fine"]
        tier = c["tier_dist"]
        lines.append(
            f"| {cell} | {c['raw_topic_count']} | {c['final_topic_count']} "
            f"| [{c['target_lo']},{c['target_hi']}] | {'✓' if c['in_interval'] else '✗'} "
            f"| {tier['CORE']}/{tier['IMPORTANT']}/{tier['LOW_FREQUENCY']} "
            f"| {c['b3_chunk_coverage']} | {c['tier_violations_dropped']} "
            f"| {c['dropped_dup_title']} | {len(c['near_dup_title_pairs'])} "
            f"| {f['extrapolated_chapter_units']}/{f['unit_interval_cap']} |"
        )
    lines.append("")
    lines.append("精规划全部 8 cell：主题违规 0、来源违规 0、区间截断 0（模型自限在区间内）、同主题近重复 0、跨主题近重复 1 对（ch1-EXTENSIVE-base）。卡片 78/78 通过 generator-output v3 schema（弃权 2，符合证据不足弃权语义）。")
    lines.append("")
    lines.append("旧架构基线（2026-09-08 生产任务，同章）：COMPACT 19 单元、EXTENSIVE 19 单元、档位无区分、COMPACT 混入 12 个 IMPORTANT。")
    lines.append("")
    lines.append("## 二、评委分数")
    lines.append("")
    lines.append("| cell | 覆盖 | 原子性 | 层级 | 单元 | | 卡:证据 | 卡:正确 | 卡:难度 | 卡:价值 | 卡数 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for cell in CELLS:
        p, card = rows[cell]["planning"], rows[cell]["card"]
        lines.append(
            f"| {cell} | {p['coverage']} | {p['atomicity']} | {p['tier']} | {p['units']} | "
            f"| {card['evidence']} | {card['correctness']} | {card['difficulty']} | {card['learning_value']} | {card['_n']} |"
        )
    lines.append("")
    lines.append("## 三、消融对比：density_assessment（base vs assess）")
    lines.append("")
    lines.append("| 对照 | base 终主题 | assess 终主题 | 增幅 | assess 自评 | 规划分变化 | 卡分变化 |")
    lines.append("|---|---|---|---|---|---|---|")
    for a, b in [
        ("ch1-COMPACT", "ch1-COMPACT"),
        ("ch1-BALANCED", "ch1-BALANCED"),
        ("ch1-EXTENSIVE", "ch1-EXTENSIVE"),
        ("ch10-EXTENSIVE", "ch10-EXTENSIVE"),
    ]:
        rb, ra = rows[f"{a}-base"], rows[f"{b}-assess"]
        nb, na = rb["coarse"]["final_topic_count"], ra["coarse"]["final_topic_count"]
        pb = rb["planning"]; pa = ra["planning"]
        cb = rb["card"]; ca = ra["card"]
        lines.append(
            f"| {a} | {nb} | {na} | +{round((na - nb) / nb * 100)}% "
            f"| {ra['coarse']['density_assessment']} "
            f"| {pb['coverage']}/{pb['atomicity']}/{pb['tier']}/{pb['units']} → "
            f"{pa['coverage']}/{pa['atomicity']}/{pa['tier']}/{pa['units']} "
            f"| 难度 {cb['difficulty']}→{ca['difficulty']} 价值 {cb['learning_value']}→{ca['learning_value']} |"
        )
    lines.append("")
    lines.append("## 四、结论")
    lines.append("")
    lines.append("1. **两阶段结构有效且稳健**：8/8 cell 命中目标区间；同章三档 25/49/81（外推单元 25/49/81），档位区分从『无』变为 3.2 倍；B3 覆盖 0.86~1.00（旧架构未测，门禁 0.60）；粗规划全视野合并使重复标题 0、近重复 1 对。")
    lines.append("2. **层级约束双保险成立**：8 个 cell tier 违规均为 0（模型在专注的粗规划任务里遵守了层级表）；即使违规，服务端确定性过滤兜底。COMPACT 三 cell 层级评委 2~3 分，无混层。")
    lines.append("3. **精规划主题契约成立**：topic_index 违规 0、来源越界 0、难度区间内自限（无需截断）；units/topic ≈ 0.75~1.0，无同主题重复。")
    lines.append("4. **density_assessment：不采纳（弃）**。理由：(a) 四组对照全部自评 RICH，章节间无区分度（ch1 密度明显低于 ch10 却同判 RICH）——信号失效；(b) 主题量 +16~30% 但规划评委分无改善，ch10 覆盖分反而 3→2；(c) baseline 已全部顶到区间上限，『欠产』不再是问题，assess 只是把量推过锚点安全域；(d) 下游生成调用量等比上涨。符合计划决策规则『中性或负面 → 弃』。")
    lines.append("5. **资产定稿改进点**（来自评委依据）：原子性全场 2 分——枚举型知识的『集合/成员』拆分与复合主题判定仍偏松，v7 粗规划提示词需把主题粒度条款写得更硬（示例补充『集合 vs 成员职责』正反例）；覆盖普遍 2 分提示低频细节仍可再挖（EXTENSIVE 档），在提示词『逐节扫描』步骤中强化图表说明与边栏的清点。")
    lines.append("6. **诚实限制**：单次运行、2 章样本、被测模型与生产不同、评委同源自评偏宽。上线后以 B5/质量指标持续观测。")
    lines.append("")
    lines.append("## 五、去留决策")
    lines.append("")
    lines.append("**v7 采用 baseline 变体（纯锚点区间，无 density_assessment）**，并按第 5 条强化主题粒度与逐节扫描措辞后定稿。")
    (BASE / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("report.md 写入完成")


if __name__ == "__main__":
    main()
