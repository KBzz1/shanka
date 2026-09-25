"""build_judge_payloads.py：问答直通双裁判盲评输入预处理（零 API）。

按 generation-quality-metrics.md「外部双裁判盲评协议」构建自包含裁判输入：
1. rubric.md 原文（同一把尺子，分数可比）；
2. 题库原文（唯一事实源）+ ground_truth 编号索引（供裁判输出 gt_id 映射）；
3. 待评卡片（position 序、稳定 card-NN 编号、含卡型/front/back/answer_boolean/explanation）；
4. 盲评：不含生产 rubric 评分、不含任何期望暗示；两裁判输入完全相同。

产出 run/<run_id>/judges/payload.md；裁判把 scores.json 写到 judges/<A|B>/scores.json。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run", required=True, help="run 目录名（run/ 下的时间戳目录）"
    )
    args = parser.parse_args()

    run_dir = WORKSPACE / "run" / args.run
    cards = json.loads((run_dir / "cards.json").read_text(encoding="utf-8"))
    gt = json.loads(
        (WORKSPACE / "fixtures" / "ground_truth.json").read_text(encoding="utf-8")
    )
    bank = (WORKSPACE / "fixtures" / "qa_bank.md").read_text(encoding="utf-8")
    rubric = (WORKSPACE / "rubric.md").read_text(encoding="utf-8")

    lines: list[str] = []
    lines.append("# 问答直通制卡盲评任务（独立裁判）")
    lines.append("")
    lines.append(
        "你是一名独立质量裁判。下面的制卡系统承诺：**资料已形成问答，AI 不命题、不改答案、"
    )
    lines.append(
        "仅做格式与卡面字段适配**。你的任务是按评分规则逐卡打分，检验这一承诺的兑现程度。"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 第一部分：评分规则（rubric，逐字执行）")
    lines.append("")
    lines.append(rubric.strip())
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 第二部分：题库原文（唯一事实源）")
    lines.append("")
    lines.append(bank.strip())
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 第三部分：题库问答对编号索引（gt_id 对照用，文本摘自上文原文）")
    lines.append("")
    for item in gt["items"]:
        stem = item.get("question") or item.get("statement") or ""
        flag = "（与上文重复出现的题）" if item.get("duplicate_of") else ""
        lines.append(f"- {item['id']} [{item['card_type']}] {_clip(stem, 60)}{flag}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"## 第四部分：待评卡片（{len(cards)} 张，position 序）")
    lines.append("")
    for i, card in enumerate(cards, start=1):
        cid = f"card-{i:02d}"
        lines.append(f"### {cid}")
        lines.append(f"- 卡型: {card.get('card_type')}")
        lines.append(f"- front: {_clip(card.get('front', ''), 800)}")
        lines.append(f"- back: {_clip(card.get('back', ''), 2000)}")
        if card.get("card_type") == "TRUE_FALSE":
            lines.append(f"- answer_boolean: {card.get('answer_boolean')}")
        explanation = card.get("explanation")
        if explanation:
            lines.append(f"- explanation: {_clip(explanation, 1200)}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 输出要求")
    lines.append("")
    lines.append("只输出一个 JSON 对象（不要多余文字、不要 markdown 代码围栏），结构：")
    lines.append(
        '`{"cards": [{"card_id": "card-01", "gt_id": "q05", '
        '"Q1": 3, "Q2": 3, "Q3": 3, "Q4": 2, "notes": "剥题号正确"}, …]}`'
    )
    lines.append("")
    lines.append(
        '- gt_id：该卡来源问答对的编号（第三部分索引）；找不到对应 = "NONE"（此时 Q1 记 0）。'
    )
    lines.append("- Q1~Q4 每维 0-3 整数，逐维独立打分；notes ≤20 字。")
    lines.append("- 每张卡都必须出现且仅出现一次，顺序与上文一致。")

    judges_dir = run_dir / "judges"
    judges_dir.mkdir(exist_ok=True)
    payload = judges_dir / "payload.md"
    payload.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 校验：payload 不得泄漏生产评分（盲评红线）
    leaked = re.search(
        r"rubric_total|rubric_score|评分覆盖", payload.read_text(encoding="utf-8")
    )
    assert not leaked, "payload 疑似泄漏生产评分字段，违反盲评协议"
    print(f"OK：{payload}（{len(cards)} 卡，{len(gt['items'])} GT 项）")
    print(f"两裁判（A/B）各评全部卡；产出写 run/{args.run}/judges/<A|B>/scores.json")


if __name__ == "__main__":
    main()
