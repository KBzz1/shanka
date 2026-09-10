"""planning_ablation.prepare_judges：卡片确定性校验 + 评委输入预处理。

1. 解析各 cell cardgen/reply_cards.txt：card 非空项按生产 card.schema.json 校验
   （QUESTION→question/answer；TRUE_FALSE→statement/answer_boolean/explanation）；
2. 构建 judge_cards/task_NNN/{card.json, spec.json, source.txt}（复用 cardgen 来源）；
3. 汇总 card_check.json（生成数/弃权数/schema 违规数）。
"""

import json
import re
from pathlib import Path

import jsonschema

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
GEN_OUTPUT_SCHEMA = json.loads(
    (REPO / "agent_evolution" / "schemas" / "v3" / "generator-output.schema.json").read_text(
        encoding="utf-8"
    )
)
# 单卡条目 schema：取 generator-output cards.items（oneOf 两分支），按 {"cards":[card]} 包装校验
_ITEMS = GEN_OUTPUT_SCHEMA["properties"]["cards"]["items"]


def parse_reply(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
        data = json.loads(re.search(r"\{.*\}", stripped, re.DOTALL).group(0))
    return data


def main() -> None:
    for cell in CELLS:
        cell_dir = RUN / cell
        gen_dir = cell_dir / "cardgen"
        data = parse_reply(gen_dir / "reply_cards.txt")
        card_wrap = {
            "type": "object",
            "required": ["cards"],
            "properties": {"cards": {"type": "array", "minItems": 0, "maxItems": 1, "items": _ITEMS}},
        }
        validator = jsonschema.Draft202012Validator(card_wrap)
        judge_dir = cell_dir / "judge_cards"
        check = {"results": len(data["results"]), "cards": 0, "null": 0, "schema_violations": 0}
        for i, result in enumerate(data["results"], start=1):
            task_src = gen_dir / f"task_{i:03d}"
            out = judge_dir / f"task_{i:03d}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "spec.json").write_text(
                (task_src / "spec.json").read_text(encoding="utf-8"), encoding="utf-8"
            )
            (out / "source.txt").write_text(
                (task_src / "source.txt").read_text(encoding="utf-8"), encoding="utf-8"
            )
            card = result.get("card")
            if card is None:
                check["null"] += 1
                (out / "card.json").write_text("null", encoding="utf-8")
                continue
            errors = list(validator.iter_errors({"cards": [card]}))
            if errors:
                check["schema_violations"] += 1
                (out / "card.json").write_text(
                    json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8"
                )
                continue
            check["cards"] += 1
            (out / "card.json").write_text(
                json.dumps(card, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        (cell_dir / "card_check.json").write_text(
            json.dumps(check, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"{cell}: {check}")


if __name__ == "__main__":
    main()
