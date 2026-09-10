"""planning_ablation.build_card_payloads：卡片生成与评委输入构建。

每 cell 从 units_validated.json 分层抽 ≤10 单元（难度轮转铺开），按生产 generator v6
的输入区块组装卡片生成任务；同时构建卡片评委输入（卡 + 其来源页原文）。

与生产的差异（报告声明）：生产 1 单元 1 调用、输出 {"cards":[≤1]}；实验 1 子代理
一次生成全部抽样单元的卡，输出 {"results":[{topic_index, card|null}]}，card 字段
遵循 card.schema.json 语义字段。
"""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUN = BASE / "run"
REPO = BASE.parents[1]
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
SAMPLE_CARDS = 10

GENERATOR_PROMPT = (
    REPO / "agent_evolution" / "prompts" / "v6" / "generator.md"
).read_text(encoding="utf-8")
CARD_SCHEMA = (
    REPO / "agent_evolution" / "schemas" / "v1" / "card.schema.json"
).read_text(encoding="utf-8")


def sample_units(units: list[dict]) -> list[dict]:
    if len(units) <= SAMPLE_CARDS:
        return list(units)
    by_diff: dict[str, list[dict]] = {"BASIC": [], "UNDERSTANDING": [], "DEEP_QUESTION": []}
    for u in units:
        by_diff[u["target_difficulty"]].append(u)
    picked: list[dict] = []
    order = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]
    while len(picked) < SAMPLE_CARDS and any(by_diff[d] for d in order):
        for d in order:
            if by_diff[d] and len(picked) < SAMPLE_CARDS:
                picked.append(by_diff[d].pop(0))
    return picked


def main() -> None:
    for cell in CELLS:
        cell_dir = RUN / cell
        chapter = json.loads((cell_dir / "payload.json").read_text(encoding="utf-8"))
        chunks_by_id = {
            c["chunk_id"]: c for c in chapter["source_chunks"]
        }
        units = json.loads((cell_dir / "units_validated.json").read_text(encoding="utf-8"))
        picked = sample_units(units)

        gen_dir = cell_dir / "cardgen"
        gen_dir.mkdir(exist_ok=True)
        system = (
            f"{GENERATOR_PROMPT.strip()}\n\n<CARD_SCHEMA>\n{CARD_SCHEMA}\n</CARD_SCHEMA>\n\n"
            "<EXPERIMENT_ADAPTION>本次为批量模式：user message 含多个独立生成单元，"
            "你必须对每个单元独立应用上述全部规则（互不复用证据、互不串内容）。输出 "
            '{"results":[{"topic_index":<整数>,"card":{...}|null},...]}，results 与输入'
            "单元一一对应且顺序一致；card 为 null 表示该单元证据不足弃权（生产语义 "
            '{"cards":[]}）。card 只含语义字段（type/question/answer 或 '
            "statement/answer_boolean/explanation），不含 front/back/页码/难度标签。</EXPERIMENT_ADAPTION>"
        )
        (gen_dir / "system.txt").write_text(system, encoding="utf-8")
        task_list = []
        for i, u in enumerate(picked, start=1):
            task_dir = gen_dir / f"task_{i:03d}"
            task_dir.mkdir(exist_ok=True)
            pages = [
                {"page_number": chunks_by_id[cid]["page_number"],
                 "content": chunks_by_id[cid]["content"]}
                for cid in u["source_chunk_ids"]
            ]
            spec = {
                "topic_index": u["topic_index"],
                "learning_objective": u["learning_objective"],
                "target_difficulty": u["target_difficulty"],
                "card_type": u["card_type"],
                "coverage_tier": u["coverage_tier"],
            }
            (task_dir / "spec.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            source_text = "\n\n".join(
                f"=== 第 {p['page_number']} 页 ===\n{p['content']}" for p in pages
            )
            (task_dir / "source.txt").write_text(source_text, encoding="utf-8")
            task_list.append(spec)
        (gen_dir / "tasks.json").write_text(
            json.dumps(task_list, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"{cell}: 卡片生成抽样 {len(picked)} 单元 "
              f"(难度 { {d: sum(1 for u in picked if u['target_difficulty']==d) for d in ('BASIC','UNDERSTANDING','DEEP_QUESTION')} })")
    print("done")


if __name__ == "__main__":
    main()
