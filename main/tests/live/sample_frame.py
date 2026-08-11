"""R1 live 60 文本块抽样框：真实 PDF 程序化抽取（纯本地文件操作，无网络、无 OCR）。

用途（task-3 brief）：从样书第 1/2/6 章内按固定 seed 分散取 20 块/章 → 输出 60 章节定义
JSON，作为 live 驱动（driver.py）的章节注入输入。抽样框在 live 前由主 Agent 审阅固定
（task-3 brief Step 2）。

规则（确定性，seed 固定即可复现）：
- 章节 = services.pdf.parser.parse_pdf 的顶层条目；目标章按名称前缀匹配（"第 1 章"/"第 2 章"/"第 6 章"）；
- 块 = 章内 20 个连续页区间：在 (start_page, end_page) 开区间内用 seed 确定性抽取 19 个切点，
  排序后与章首尾组成 20 块（全覆盖、不重叠、每块 ≥ 1 页）；要求章页数 ≥ 20；
- 难度按全局块序循环 easy/medium/hard（driver 映射 COMPACT/BALANCED/EXTENSIVE）；
- file_id 为占位空串（driver 注入真实上传 file_id 时填写）。

用法：cd main && conda run -n shanka-backend python -m tests.live.sample_frame \
  --out /tmp/r1-frame.json [--pdf <样书路径>] [--seed 20260811]
"""

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

from services.pdf.parser import ChapterInfo, parse_pdf

# 样书路径（与 tests/acceptance/test_acceptance_r1_paths.py SAMPLE 同源：只读引用，不复制不提交）
_DEFAULT_PDF = Path("/home/kbzz1/shanka_backend/res/AI-Agents-in-Depth-zh-CN.pdf")

_TARGET_CHAPTERS = ("第 1 章", "第 2 章", "第 6 章")  # 名称前缀（样书顶层条目实测）
_BLOCKS_PER_CHAPTER = 20
_BLOCK_COUNT = _BLOCKS_PER_CHAPTER * len(_TARGET_CHAPTERS)
_DIFFICULTY_CYCLE = ("easy", "medium", "hard")
_DEFAULT_SEED = 20260811  # task-3 brief：固定 seed（如 20260811）


def _ordinal(name: str) -> int:
    """章名中的序号：'第 1 章 AI Agent 入门' → 1。"""
    match = re.search(r"第\s*(\d+)\s*章", name)
    if match is None:
        raise ValueError(f"章名无序号: {name!r}")
    return int(match.group(1))


def _select_chapters(chapters: list[ChapterInfo]) -> list[tuple[ChapterInfo, int]]:
    """按名称前缀选第 1/2/6 章；每章必须恰好一个，否则报错（章节结构漂移即失败）。"""
    selected: list[tuple[ChapterInfo, int]] = []
    for target in _TARGET_CHAPTERS:
        matches = [ch for ch in chapters if ch["name"].startswith(target)]
        if len(matches) != 1:
            available = "；".join(ch["name"] for ch in chapters)
            raise ValueError(
                f"目标章 {target!r} 应恰好 1 个，实际 {len(matches)} 个；可用章：{available}"
            )
        selected.append((matches[0], _ordinal(matches[0]["name"])))
    return selected


def _sample_blocks(
    chapter: ChapterInfo, ordinal: int, *, seed: int, blocks_per_chapter: int
) -> list[dict[str, Any]]:
    """章内分散取块：seed 确定性抽取 blocks_per_chapter-1 个切点 → 连续页区间块（≥1 页）。"""
    start, end = chapter["start_page"], chapter["end_page"]
    span = end - start + 1
    if span < blocks_per_chapter:
        raise ValueError(
            f"章 {chapter['name']!r} 页数 {span} < 块数 {blocks_per_chapter}，无法分散取块"
        )
    rng = random.Random(f"{seed}-{ordinal}")  # 每章独立子随机源（seed 固定 → 结果固定）
    cuts = sorted(rng.sample(range(start + 1, end), blocks_per_chapter - 1))
    boundaries = [start, *cuts, end + 1]
    blocks: list[dict[str, Any]] = []
    for i in range(blocks_per_chapter):
        blocks.append(
            {
                "chapter_name": chapter["name"],
                "chapter_ordinal": ordinal,
                "start_page": boundaries[i],
                "end_page": boundaries[i + 1] - 1,
                "file_id": "",  # 占位：driver 注入真实上传 file_id
            }
        )
    return blocks


def build_frame(*, pdf_path: Path, seed: int) -> dict[str, Any]:
    """构建抽样框：解析样书 → 选章 → 分散取块 → 难度循环。纯本地文件操作。"""
    if not pdf_path.exists():
        raise FileNotFoundError(f"样书不存在: {pdf_path}")
    _text_sample, chapters = parse_pdf(pdf_path)
    if not _text_sample.strip():
        raise ValueError("样书无可提取文本层（抽样框依赖文本层）")
    selected = _select_chapters(chapters)
    blocks: list[dict[str, Any]] = []
    for chapter, ordinal in selected:
        blocks.extend(_sample_blocks(chapter, ordinal, seed=seed, blocks_per_chapter=20))
    for i, block in enumerate(blocks):
        block["index"] = i + 1
        block["difficulty"] = _DIFFICULTY_CYCLE[i % len(_DIFFICULTY_CYCLE)]
    total_pages = max(ch["end_page"] for ch in chapters)
    return {
        "frame_version": 1,
        "seed": seed,
        "pdf": pdf_path.name,
        "total_pages": total_pages,
        "block_count": len(blocks),
        "blocks": blocks,
    }


def _print_summary(frame: dict[str, Any]) -> None:
    print(f"抽样框: seed={frame['seed']} pdf={frame['pdf']} 总页数={frame['total_pages']}")
    by_chapter: dict[str, list[dict[str, Any]]] = {}
    for block in frame["blocks"]:
        by_chapter.setdefault(block["chapter_name"], []).append(block)
    for name, blocks in by_chapter.items():
        pages = f"{blocks[0]['start_page']}-{blocks[-1]['end_page']}"
        difficulty = "".join(b["difficulty"][0].upper() for b in blocks)
        print(f"  {name}（页 {pages}）：{len(blocks)} 块，难度序 {difficulty}")
    print(f"共 {frame['block_count']} 块（第 1/2/6 章 × 20）")


def main() -> None:
    parser = argparse.ArgumentParser(description="R1 live 60 文本块抽样框（纯本地文件操作）")
    parser.add_argument("--pdf", type=Path, default=_DEFAULT_PDF, help="样书 PDF 路径")
    parser.add_argument(
        "--out", type=Path, required=True, help="输出 JSON 路径（如 /tmp/r1-frame.json）"
    )
    parser.add_argument(
        "--seed", type=int, default=_DEFAULT_SEED, help="固定 seed（默认 20260811）"
    )
    args = parser.parse_args()

    frame = build_frame(pdf_path=args.pdf, seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(frame, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(frame)
    print(f"已写入: {args.out}")


if __name__ == "__main__":
    main()
