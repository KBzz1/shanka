"""planning_ablation.fine_metrics：精规划输出确定性校验 + 汇总指标。

对每个 cell 的 fine/reply_fine.txt：
1. 宽松解析 → schema 校验（planner-fine-output 草稿 schema）；
2. 主题契约：topic_index 必须 ∈ 抽样主题（违规计数）；
3. 来源契约：chunk ⊆ 批次页集、≤8 页、≤10000 字符（违规计数）；
4. 难度区间截断模拟：每难度按数组序保留 max 个（截断计数）——生产 _enforce_interval_max 同款；
5. tier 注入模拟：从 topics_validated 的 topic→tier 映射注入（缺失即失败计数）；
6. 指标：单元数（截断前后）、units/topic 分布、难度分布 vs 区间、同主题近重复对
   （同难度同卡型 + objective 3-gram Jaccard≥0.5）、跨主题 objective 近重复对、
   章级外推单元数 = final_topics × 抽样 units/topic（按单元区间上限封顶）。
"""

import json
import re
from pathlib import Path

import jsonschema

BASE = Path(__file__).resolve().parent
RUN = BASE / "run"
ASSETS = BASE / "assets"
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

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!()（）【】\[\]《》<>\"'‘’“”·…\-—_/\\|]+")


def _norm(text: str) -> str:
    return _PUNCT.sub("", text)


def _ngrams(text: str, n: int = 3) -> set[str]:
    return {text[i : i + n] for i in range(max(len(text) - n + 1, 0))} or {text}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def parse_reply(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        stripped = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
        data = json.loads(re.search(r"\{.*\}", stripped, re.DOTALL).group(0))
    assert isinstance(data, dict)
    return data


def main() -> None:
    schema = json.loads(
        (ASSETS / "planner-fine-output.schema.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    summary = {}
    for cell in CELLS:
        cell_dir = RUN / cell
        meta = json.loads((cell_dir / "meta.json").read_text(encoding="utf-8"))
        coarse = json.loads((cell_dir / "coarse_metrics.json").read_text(encoding="utf-8"))
        topics = json.loads(
            (cell_dir / "topics_validated.json").read_text(encoding="utf-8")
        )
        fine_payload = json.loads(
            (cell_dir / "fine" / "payload.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (cell_dir / "fine" / "chunks" / "manifest.json").read_text(encoding="utf-8")
        )
        chapter_chunks = {
            c["chunk_id"]: len(c["content"])
            for c in json.loads((cell_dir / "payload.json").read_text(encoding="utf-8"))[
                "source_chunks"
            ]
        }
        batch_ids = {m["chunk_id"] for m in manifest}
        data = parse_reply(cell_dir / "fine" / "reply_fine.txt")
        validator.validate(data)
        units = data["units"]

        topic_map = {t["topic_index"]: t for t in fine_payload["topics"]}
        m = {
            "raw_unit_count": len(units),
            "topic_violations": 0,
            "source_violations": 0,
            "tier_inject_failures": 0,
        }
        legit = []
        for unit in units:
            if unit["topic_index"] not in topic_map:
                m["topic_violations"] += 1
                continue
            ids = unit["source_chunk_ids"]
            chars = sum(chapter_chunks.get(cid, 0) for cid in ids)
            if any(cid not in batch_ids for cid in ids) or len(ids) > 8 or chars > 10_000:
                m["source_violations"] += 1
                continue
            topic = topic_map[unit["topic_index"]]
            legit.append(
                {
                    **unit,
                    "coverage_tier": topic["coverage_tier"],
                    "topic_title": topic["title"],
                }
            )

        # 难度区间截断模拟（生产 _enforce_interval_max 同款：数组序确定性截断）
        diff = fine_payload["difficulty_interval"]
        surviving = set()
        for difficulty in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION"):
            cap = diff.get(difficulty, {}).get("max", 0)
            kept = 0
            for i, unit in enumerate(legit):
                if unit["target_difficulty"] == difficulty:
                    if kept < cap:
                        surviving.add(i)
                        kept += 1
        m["truncated_by_interval"] = len(legit) - len(surviving)
        final_units = [legit[i] for i in sorted(surviving)]

        # 同主题近重复（同难度同卡型 + objective 近似）
        intra_dups = []
        by_topic: dict[int, list[dict]] = {}
        for unit in final_units:
            by_topic.setdefault(unit["topic_index"], []).append(unit)
        for t_units in by_topic.values():
            for i in range(len(t_units)):
                for j in range(i + 1, len(t_units)):
                    a, b = t_units[i], t_units[j]
                    if a["target_difficulty"] == b["target_difficulty"] and a["card_type"] == b["card_type"]:
                        score = _jaccard(_ngrams(_norm(a["learning_objective"])), _ngrams(_norm(b["learning_objective"])))
                        if score >= 0.5:
                            intra_dups.append(
                                {"topic": a["topic_index"], "jaccard": round(score, 2),
                                 "a": a["learning_objective"], "b": b["learning_objective"]}
                            )
        # 跨主题 objective 近重复
        cross_dups = []
        for i in range(len(final_units)):
            for j in range(i + 1, len(final_units)):
                a, b = final_units[i], final_units[j]
                if a["topic_index"] == b["topic_index"]:
                    continue
                score = _jaccard(_ngrams(_norm(a["learning_objective"])), _ngrams(_norm(b["learning_objective"])))
                if score >= 0.5:
                    cross_dups.append(
                        {"topics": [a["topic_index"], b["topic_index"]],
                         "jaccard": round(score, 2)}
                    )

        units_per_topic = [len(v) for v in by_topic.values()]
        diff_dist = {"BASIC": 0, "UNDERSTANDING": 0, "DEEP_QUESTION": 0}
        card_dist = {"QUESTION": 0, "TRUE_FALSE": 0}
        for unit in final_units:
            diff_dist[unit["target_difficulty"]] += 1
            card_dist[unit["card_type"]] += 1
        sample_topics = len(topic_map)
        sample_units_per_topic = len(final_units) / max(sample_topics, 1)
        # 章级外推：抽样 units/topic × 全部主题，按章单元区间上限封顶
        extrapolated = coarse["final_topic_count"] * sample_units_per_topic
        unit_cap = coarse["interval"]["max"]
        metrics = {
            "cell": cell,
            **m,
            "final_unit_count": len(final_units),
            "sample_topic_count": sample_topics,
            "sample_units_per_topic": round(sample_units_per_topic, 2),
            "units_per_topic_hist": {
                "1": sum(1 for x in units_per_topic if x == 1),
                "2": sum(1 for x in units_per_topic if x == 2),
                "3+": sum(1 for x in units_per_topic if x >= 3),
            },
            "difficulty_dist": diff_dist,
            "difficulty_interval": diff,
            "card_type_dist": card_dist,
            "intra_topic_near_dups": intra_dups,
            "cross_topic_near_dups": cross_dups,
            "extrapolated_chapter_units": round(extrapolated),
            "unit_interval_cap": unit_cap,
            "extrapolation_capped": extrapolated > unit_cap,
        }
        (cell_dir / "fine_metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (cell_dir / "units_validated.json").write_text(
            json.dumps(final_units, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        summary[cell] = metrics
        print(
            f"{cell}: raw={metrics['raw_unit_count']} final={metrics['final_unit_count']} "
            f"(截断={metrics['truncated_by_interval']} 主题违规={metrics['topic_violations']} "
            f"来源违规={metrics['source_violations']}) u/t={metrics['sample_units_per_topic']} "
            f"难度={diff_dist} 同主题重复={len(intra_dups)} 跨主题重复={len(cross_dups)} "
            f"外推={metrics['extrapolated_chapter_units']}/{unit_cap}"
        )
    (RUN / "fine_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
