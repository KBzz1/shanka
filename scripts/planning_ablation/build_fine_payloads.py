"""planning_ablation.build_fine_payloads：粗规划输出确定性校验 + 精规划批次构建。

对每个 cell 的 reply_coarse.txt：
1. 宽松解析（容忍围栏）→ schema 校验（jsonschema，按 variant 选 schema）；
2. 确定性过滤：未知 chunk 引用 / 超 limits / tier 不在模式允许集（违规计数）/ 标题规范化去重；
3. 截断：baseline 截到 topic_interval.max；assess 截到钳制上限 ceil(1.5×max)；
4. topic_index 1..N 分配；写 topics_validated.json 与 coarse_metrics.json（含 B3 覆盖率、
   标题近重复对）。
5. 分层抽样 ≤20 主题构建精规划批次（难度区间按抽样份额缩放），输出 fine/ 目录。

精规划抽样口径（与生产的差异，报告须声明）：生产 8 主题/批 + 20k 字符上限分批；
实验每 cell 一个抽样批、无字符上限（子代理上下文不受 token 预算约束）。
"""

import json
import math
import re
import sqlite3
import sys
from pathlib import Path

import jsonschema

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "main"))

DB = _REPO / "main" / "data" / "shanka.db"
BASE = Path(__file__).resolve().parent
RUN = BASE / "run"
ASSETS = BASE / "assets"

TIER_ALLOWED = {
    "COMPACT": {"CORE"},
    "BALANCED": {"CORE", "IMPORTANT"},
    "EXTENSIVE": {"CORE", "IMPORTANT", "LOW_FREQUENCY"},
}
CELLS = [
    ("ch1-COMPACT-base", "base"),
    ("ch1-COMPACT-assess", "assess"),
    ("ch1-BALANCED-base", "base"),
    ("ch1-BALANCED-assess", "assess"),
    ("ch1-EXTENSIVE-base", "base"),
    ("ch1-EXTENSIVE-assess", "assess"),
    ("ch10-EXTENSIVE-base", "base"),
    ("ch10-EXTENSIVE-assess", "assess"),
]
SAMPLE_TARGET = 20
NEAR_DUP_THRESHOLD = 0.6

_PUNCT = re.compile(r"[\s，。、；：？！,.;:?!()（）【】\[\]《》<>\"'‘’“”·…\-—_/\\|]+")


def _norm_title(title: str) -> str:
    return _PUNCT.sub("", title).lower()


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
        try:
            data = json.loads(stripped)
        except ValueError:
            match = re.search(r"\{.*\}", stripped, re.DOTALL)
            if match is None:
                raise
            data = json.loads(match.group(0))
    assert isinstance(data, dict)
    return data


def chapter_chunk_index(conn: sqlite3.Connection, chapter_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.chunk_id, t.chunk_seq, t.char_count FROM text_chunks t "
        "JOIN chapters c ON c.material_id = t.material_id "
        "WHERE c.chapter_id = ? AND t.chunk_seq BETWEEN c.start_page AND c.end_page "
        "ORDER BY t.chunk_seq",
        (chapter_id,),
    ).fetchall()
    return [
        {"chunk_id": r[0], "pos": i, "char_count": r[2]} for i, r in enumerate(rows)
    ]


def validate_and_filter(data: dict, meta: dict, chunk_index: list[dict]) -> tuple[list, dict]:
    variant = meta["variant"]
    mode = meta["coverage_mode"]
    schema_name = (
        "planner-coarse-output.schema.json"
        if variant == "base"
        else "planner-coarse-output-assess.schema.json"
    )
    schema = json.loads((ASSETS / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(data)

    by_id = {c["chunk_id"]: c for c in chunk_index}
    topics_raw = data["topics"]
    m = {
        "density_assessment": data.get("density_assessment"),
        "raw_topic_count": len(topics_raw),
        "dropped_unknown_chunk": 0,
        "dropped_limits": 0,
        "tier_violations_dropped": 0,
        "dropped_dup_title": 0,
        "truncated_to_cap": 0,
    }
    allowed = TIER_ALLOWED[mode]
    seen_titles: set[str] = set()
    kept: list[dict] = []
    for topic in topics_raw:
        ids = topic["source_chunk_ids"]
        if any(cid not in by_id for cid in ids):
            m["dropped_unknown_chunk"] += 1
            continue
        if len(ids) > 8 or sum(by_id[cid]["char_count"] for cid in ids) > 10_000:
            m["dropped_limits"] += 1
            continue
        if topic["coverage_tier"] not in allowed:
            m["tier_violations_dropped"] += 1
            continue
        norm = _norm_title(topic["title"])
        if not norm or norm in seen_titles:
            m["dropped_dup_title"] += 1
            continue
        seen_titles.add(norm)
        kept.append(
            {
                "title": topic["title"],
                "coverage_tier": topic["coverage_tier"],
                "source_chunk_ids": ids,
                "_norm": norm,
                "_min_pos": min(by_id[cid]["pos"] for cid in ids),
            }
        )
    interval = meta["topic_interval"]
    cap = interval["max"] if variant == "base" else math.ceil(interval["max"] * 1.5)
    if len(kept) > cap:
        m["truncated_to_cap"] = len(kept) - cap
        kept = kept[:cap]
    for i, topic in enumerate(kept, start=1):
        topic["topic_index"] = i
    return kept, m


def coarse_metrics(kept: list[dict], meta: dict, chunk_index: list[dict], raw: dict) -> dict:
    tier_dist: dict[str, int] = {"CORE": 0, "IMPORTANT": 0, "LOW_FREQUENCY": 0}
    for topic in kept:
        tier_dist[topic["coverage_tier"]] += 1
    covered = {cid for topic in kept for cid in topic["source_chunk_ids"]}
    norms = [(t["topic_index"], t["_norm"]) for t in kept]
    near_dups = []
    for i in range(len(norms)):
        for j in range(i + 1, len(norms)):
            score = _jaccard(_ngrams(norms[i][1]), _ngrams(norms[j][1]))
            if score >= NEAR_DUP_THRESHOLD:
                near_dups.append({"a": norms[i][0], "b": norms[j][0], "jaccard": round(score, 2)})
    interval = meta["topic_interval"]
    variant = meta["variant"]
    target_lo = interval["min"]
    target_hi = (
        interval["max"] if variant == "base" else round(interval["max"] * 1.3)
    )
    return {
        "cell": meta["cell"],
        "raw_topic_count": raw.get("raw_topic_count"),
        "final_topic_count": len(kept),
        "tier_dist": tier_dist,
        "interval": interval,
        "target_lo": target_lo,
        "target_hi": target_hi,
        "in_interval": target_lo <= len(kept) <= target_hi,
        "b3_chunk_coverage": round(len(covered) / len(chunk_index), 3),
        "near_dup_title_pairs": near_dups,
        "pages_per_topic_avg": round(
            sum(len(t["source_chunk_ids"]) for t in kept) / max(len(kept), 1), 2
        ),
    }


def stratified_sample(kept: list[dict]) -> list[dict]:
    if len(kept) <= SAMPLE_TARGET:
        return list(kept)
    idxs = {0, 1, 2, 3}
    extra = SAMPLE_TARGET - len(idxs)
    for j in range(extra):
        idxs.add(round(j * (len(kept) - 1) / max(extra - 1, 1)))
    pos = 0
    while len(idxs) < SAMPLE_TARGET and pos < len(kept):
        idxs.add(pos)
        pos += 1
    chosen = [kept[k] for k in sorted(idxs)][:SAMPLE_TARGET]
    present = {t["coverage_tier"] for t in chosen}
    for tier in ("CORE", "IMPORTANT", "LOW_FREQUENCY"):
        if tier not in present:
            swap = next((t for t in chosen if t["coverage_tier"] != tier), None)
            alt = next((t for t in kept if t["coverage_tier"] == tier and t not in chosen), None)
            if swap is not None and alt is not None:
                chosen[chosen.index(swap)] = alt
    return sorted(chosen, key=lambda t: t["topic_index"])


def scaled_interval(chapter_diff: dict, share: float) -> dict:
    out = {}
    for d, bounds in chapter_diff.items():
        if bounds["max"] <= 0:
            out[d] = {"min": 0, "max": 0}
        else:
            out[d] = {
                "min": int(bounds["min"] * share),
                "max": max(1, round(bounds["max"] * share)),
            }
    return out


def build_fine(cell_dir: Path, kept: list[dict], sample: list[dict], meta: dict,
               chunk_index: list[dict]) -> dict:
    fine_dir = cell_dir / "fine"
    fine_dir.mkdir(exist_ok=True)
    wanted = {cid for t in sample for cid in t["source_chunk_ids"]}
    positions = {c["chunk_id"]: c["pos"] for c in chunk_index}
    involved = sorted({positions[cid] for cid in wanted})
    margin = 1
    page_set = set()
    for pos in involved:
        for p in range(max(pos - margin, 0), min(pos + margin, len(chunk_index) - 1) + 1):
            page_set.add(p)
    share = len(sample) / max(len(kept), 1)
    diff = scaled_interval(meta["difficulty_interval"], share)
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    payload = {
        "chapter": {"name": meta["chapter_name"]},
        "coverage_mode": meta["coverage_mode"],
        "topics": [
            {
                "topic_index": t["topic_index"],
                "title": t["title"],
                "coverage_tier": t["coverage_tier"],
                "source_chunk_ids": t["source_chunk_ids"],
            }
            for t in sample
        ],
        "difficulty_interval": diff,
        "limits": {
            "max_source_chunks_per_unit": 8,
            "max_source_chars_per_unit": 10_000,
        },
        "custom_requirements": None,
    }
    prompt = (ASSETS / "planner-fine.md").read_text(encoding="utf-8").strip()
    schema = (ASSETS / "planner-fine-output.schema.json").read_text(encoding="utf-8")
    system = f"{prompt}\n\n<PLANNER_OUTPUT_SCHEMA>\n{schema}\n</PLANNER_OUTPUT_SCHEMA>"
    (fine_dir / "system.txt").write_text(system, encoding="utf-8")
    (fine_dir / "payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    skeleton = {**payload, "source_chunks": "<见 chunks/ 目录，文件名序即页序>"}
    (fine_dir / "payload_skeleton.json").write_text(
        json.dumps(skeleton, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    chunks_dir = fine_dir / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    manifest = []
    for i, pos in enumerate(sorted(page_set), start=1):
        info = chunk_index[pos]
        row = conn.execute(
            "SELECT content, page_number FROM text_chunks WHERE chunk_id = ?",
            (info["chunk_id"],),
        ).fetchone()
        fname = f"{i:03d}.txt"
        (chunks_dir / fname).write_text(row[0], encoding="utf-8")
        manifest.append(
            {"file": fname, "chunk_id": info["chunk_id"], "page_number": row[1]}
        )
    (chunks_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    conn.close()
    return {
        "sample_topic_count": len(sample),
        "sample_share": round(share, 3),
        "sample_difficulty_interval": diff,
        "batch_chunk_count": len(page_set),
    }


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    summary = {}
    for cell, variant in CELLS:
        cell_dir = RUN / cell
        meta = json.loads((cell_dir / "meta.json").read_text(encoding="utf-8"))
        payload = json.loads((cell_dir / "payload.json").read_text(encoding="utf-8"))
        chunk_index = chapter_chunk_index(conn, payload["chapter"]["chapter_id"])
        data = parse_reply(cell_dir / "reply_coarse.txt")
        kept, raw_metrics = validate_and_filter(data, meta, chunk_index)
        metrics = {**raw_metrics, **coarse_metrics(kept, meta, chunk_index, raw_metrics)}
        sample = stratified_sample(kept)
        fine_info = build_fine(cell_dir, kept, sample, meta, chunk_index)
        metrics["fine"] = fine_info
        (cell_dir / "coarse_metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (cell_dir / "topics_validated.json").write_text(
            json.dumps(
                [
                    {k: v for k, v in t.items() if not k.startswith("_")}
                    for t in kept
                ],
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        summary[cell] = metrics
        print(
            f"{cell}: raw={metrics['raw_topic_count']} final={metrics['final_topic_count']} "
            f"(目标[{metrics['target_lo']},{metrics['target_hi']}] 命中={metrics['in_interval']}) "
            f"tier={metrics['tier_dist']} B3={metrics['b3_chunk_coverage']} "
            f"tier违规={metrics['tier_violations_dropped']} 重复标题={metrics['dropped_dup_title']} "
            f"近重复对={len(metrics['near_dup_title_pairs'])} 截断={metrics['truncated_to_cap']} "
            f"| 精规划抽样 {fine_info['sample_topic_count']} 主题/{fine_info['batch_chunk_count']} 页"
        )
    (RUN / "coarse_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    conn.close()


if __name__ == "__main__":
    main()
