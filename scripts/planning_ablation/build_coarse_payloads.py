"""planning_ablation.build_coarse_payloads：消融实验粗规划消息构建。

从 main/data/shanka.db 读取真实章节页文本（任务实际使用的 material），用生产
services/generation/quota.py 的密度制函数计算各 cell 的主题数量区间，按生产
infra/llm/prompts.py 同款信封与序列化规则产出 system/user 消息文件，供子代理
充当被测模型消费。

cell 矩阵：ch1 × {COMPACT,BALANCED,EXTENSIVE} × {base,assess} + ch10 × EXTENSIVE × {base,assess}。
难度比例统一 40/40/20（对照变量只留 coverage_mode 与 variant）。
"""

import json
import sqlite3
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "main"))

from services.generation.quota import (  # noqa: E402
    DEFAULT_CARDS_PER_10K,
    difficulty_interval,
    interval_for_chapter,
)

DB = _REPO / "main" / "data" / "shanka.db"
OUT = Path(__file__).resolve().parent / "run"
ASSETS = Path(__file__).resolve().parent / "assets"

CHAPTERS = {"ch1": "第 1 章 AI Agent 入门", "ch10": "第 10 章 多 Agent 协作"}
CELLS = [
    ("ch1", "COMPACT", "base"),
    ("ch1", "COMPACT", "assess"),
    ("ch1", "BALANCED", "base"),
    ("ch1", "BALANCED", "assess"),
    ("ch1", "EXTENSIVE", "base"),
    ("ch1", "EXTENSIVE", "assess"),
    ("ch10", "EXTENSIVE", "base"),
    ("ch10", "EXTENSIVE", "assess"),
]
RATIO = (0.40, 0.40, 0.20)  # basic / understanding / deep_question
LIMITS = {"max_source_chunks_per_topic": 8, "max_source_chars_per_topic": 10_000}


def safe_json_dumps(payload: object) -> str:
    """与 infra/llm/prompts.safe_json_dumps 同款：确定性序列化 + 信封边界转义。"""
    rendered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return rendered.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def resolve_material(conn: sqlite3.Connection) -> str:
    """取最近任务快照实际引用的 material_id（章节表存在重复 material，锁定任务用过的）。"""
    row = conn.execute(
        "SELECT selected_chapters FROM tasks ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    for (snap,) in row:
        try:
            entries = json.loads(snap)
            for e in entries:
                if e.get("material_id"):
                    return str(e["material_id"])
        except (ValueError, TypeError, AttributeError):
            continue
    raise SystemExit("无法从任务快照解析 material_id")


def load_chapter(conn: sqlite3.Connection, material: str, name: str) -> dict:
    ch = conn.execute(
        "SELECT chapter_id, name, start_page, end_page FROM chapters "
        "WHERE material_id = ? AND name = ? LIMIT 1",
        (material, name),
    ).fetchone()
    if ch is None:
        raise SystemExit(f"章节不存在: {name}")
    chunks = conn.execute(
        "SELECT chunk_id, chunk_seq, page_number, char_count, content FROM text_chunks "
        "WHERE material_id = ? AND chunk_seq BETWEEN ? AND ? ORDER BY chunk_seq",
        (material, ch[2], ch[3]),
    ).fetchall()
    return {
        "chapter_id": ch[0],
        "name": ch[1],
        "start_page": ch[2],
        "end_page": ch[3],
        "chunks": [
            {
                "chunk_id": c[0],
                "chunk_seq": c[1],
                "page_number": c[2],
                "char_count": c[3],
                "content": c[4],
            }
            for c in chunks
        ],
    }


def main() -> None:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    material = resolve_material(conn)
    print(f"material_id = {material}")
    chapters = {key: load_chapter(conn, material, name) for key, name in CHAPTERS.items()}
    for key, ch in chapters.items():
        chars = sum(c["char_count"] for c in ch["chunks"])
        print(f"{key} {ch['name']}: p{ch['start_page']}-{ch['end_page']} "
              f"{len(ch['chunks'])}块 {chars}字")

    prompt_base = (ASSETS / "planner-coarse-baseline.md").read_text(encoding="utf-8")
    prompt_assess = (ASSETS / "planner-coarse-assess.md").read_text(encoding="utf-8")
    schema_base = (ASSETS / "planner-coarse-output.schema.json").read_text(encoding="utf-8")
    schema_assess = (ASSETS / "planner-coarse-output-assess.schema.json").read_text(
        encoding="utf-8"
    )

    for ch_key, mode, variant in CELLS:
        ch = chapters[ch_key]
        chars = sum(c["char_count"] for c in ch["chunks"])
        lo, hi = interval_for_chapter(chars, mode, DEFAULT_CARDS_PER_10K)
        cell = f"{ch_key}-{mode}-{variant}"
        out_dir = OUT / cell
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "chapter": {
                "chapter_id": ch["chapter_id"],
                "name": ch["name"],
                "start_page": ch["start_page"],
                "end_page": ch["end_page"],
            },
            "coverage_mode": mode,
            "topic_interval": {"min": lo, "max": hi},
            "limits": LIMITS,
            "source_chunks": [
                {
                    "chunk_id": c["chunk_id"],
                    "page_number": c["page_number"],
                    "content": c["content"],
                }
                for c in ch["chunks"]
            ],
            "custom_requirements": None,
        }
        prompt = prompt_base if variant == "base" else prompt_assess
        schema = schema_base if variant == "base" else schema_assess
        system = f"{prompt.strip()}\n\n<PLANNER_COARSE_OUTPUT_SCHEMA>\n{schema}\n</PLANNER_COARSE_OUTPUT_SCHEMA>"
        user = f"<PLANNER_COARSE_INPUT>{safe_json_dumps(payload)}</PLANNER_COARSE_INPUT>"
        (out_dir / "system.txt").write_text(system, encoding="utf-8")
        (out_dir / "user.txt").write_text(user, encoding="utf-8")
        (out_dir / "payload.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        # 子代理可读形态：骨架（不含 source_chunks）+ 逐页原文文件（自然换行，避免
        # Read 工具截断长 JSON 行；文件名序即页序）
        skeleton = {**payload, "source_chunks": "<见 chunks/ 目录，文件名序即页序>"}
        (out_dir / "payload_skeleton.json").write_text(
            json.dumps(skeleton, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        chunks_dir = out_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)
        manifest = []
        for i, c in enumerate(ch["chunks"], start=1):
            fname = f"{i:03d}.txt"
            (chunks_dir / fname).write_text(c["content"], encoding="utf-8")
            manifest.append(
                {"file": fname, "chunk_id": c["chunk_id"], "page_number": c["page_number"]}
            )
        (chunks_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        diff = difficulty_interval((lo, hi), *RATIO)
        (out_dir / "meta.json").write_text(
            json.dumps(
                {
                    "cell": cell,
                    "chapter": ch_key,
                    "chapter_name": ch["name"],
                    "coverage_mode": mode,
                    "variant": variant,
                    "chars": chars,
                    "chunk_count": len(ch["chunks"]),
                    "topic_interval": {"min": lo, "max": hi},
                    "clamp_interval": {
                        "min": max(0, int(lo * 0.5)),
                        "max": int(-(-int(hi * 1.5) // 1)),
                    },
                    "difficulty_interval": diff,
                    "difficulty_ratio": {"basic": 40, "understanding": 40, "deep": 20},
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"{cell}: topic_interval=[{lo},{hi}] 输入字符={len(user)}")
    print("done")


if __name__ == "__main__":
    main()
