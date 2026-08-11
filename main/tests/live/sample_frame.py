"""R1 live 60 文本块抽样框：真实 PDF 程序化抽取（纯本地文件操作，无网络、无 OCR）。

用途（task-3 brief）：从样书第 1/2/6 章内按固定 seed 分散取 20 块/章 → 输出 60 章节定义
JSON，作为 live 驱动（driver.py）的章节注入输入。抽样框在 live 前由主 Agent 审阅固定
（task-3 brief Step 2）。

规则（确定性，seed 固定即可复现）：
- 章节 = services.pdf.parser.parse_pdf 的顶层条目；目标章按名称前缀匹配（"第 1 章"/"第 2 章"/"第 6 章"）；
- 块 = 章内 20 个连续页区间：在 (start_page, end_page) 开区间内用 seed 确定性抽取 19 个切点，
  排序后与章首尾组成 20 块（全覆盖、不重叠、每块 ≥ 1 页）；要求 end - start ≥ 块数
  （每章 20 块需 19 个切点，end - start = 20 恰好可取）；
- 难度按章分配：每章 20 块 = easy 8 + medium 8 + hard 4（章内顺序 easy×8 → medium×8 → hard×4），
  全局 24/24/12（driver 映射 COMPACT/BALANCED/EXTENSIVE）；
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
# 每章难度分配（20 块 = easy 8 + medium 8 + hard 4 → 全局 easy 24 / medium 24 / hard 12，计划冻结）
_DIFFICULTY_PER_CHAPTER: tuple[tuple[str, int], ...] = (("easy", 8), ("medium", 8), ("hard", 4))
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
    # 切点取自 (start, end) 开区间（end - start - 1 个候选）；end - start >= 块数
    # 才取得出 blocks_per_chapter - 1 个切点（M4：span==blocks_per_chapter 时旧守卫放行后 sample 必炸）
    if end - start < blocks_per_chapter:
        span = end - start + 1
        raise ValueError(
            f"章 {chapter['name']!r} 页数 {span}（end-start={end - start}）不足以分散取 "
            f"{blocks_per_chapter} 块（需 end-start ≥ {blocks_per_chapter}）"
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


def _difficulty_for_index(index_in_chapter: int) -> str:
    """章内第 index_in_chapter 块难度（0-based）：按 _DIFFICULTY_PER_CHAPTER 分段 8/8/4。"""
    remaining = index_in_chapter
    for name, count in _DIFFICULTY_PER_CHAPTER:
        if remaining < count:
            return name
        remaining -= count
    raise ValueError(f"章内块序号越界: {index_in_chapter}")


def build_frame(*, pdf_path: Path, seed: int) -> dict[str, Any]:
    """构建抽样框：解析样书 → 选章 → 分散取块 → 按章分配难度。纯本地文件操作。"""
    if not pdf_path.exists():
        raise FileNotFoundError(f"样书不存在: {pdf_path}")
    _text_sample, chapters = parse_pdf(pdf_path)
    if not _text_sample.strip():
        raise ValueError("样书无可提取文本层（抽样框依赖文本层）")
    selected = _select_chapters(chapters)
    blocks: list[dict[str, Any]] = []
    for chapter, ordinal in selected:
        chapter_blocks = _sample_blocks(
            chapter, ordinal, seed=seed, blocks_per_chapter=_BLOCKS_PER_CHAPTER
        )
        for i, block in enumerate(chapter_blocks):
            block["difficulty"] = _difficulty_for_index(i)
        blocks.extend(chapter_blocks)
    for i, block in enumerate(blocks):
        block["index"] = i + 1
    # 计划冻结：全局难度 easy/medium/hard = 24/24/12（3 章 × 8/8/4）；漂移即失败
    expected = {name: count * len(_TARGET_CHAPTERS) for name, count in _DIFFICULTY_PER_CHAPTER}
    actual = {name: sum(1 for b in blocks if b["difficulty"] == name) for name in expected}
    if actual != expected:
        raise ValueError(f"难度分布漂移: {actual}（计划冻结 {expected}）")
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
        counts = {
            d: sum(1 for b in blocks if b["difficulty"] == d) for d, _ in _DIFFICULTY_PER_CHAPTER
        }
        print(f"  {name}（页 {pages}）：{len(blocks)} 块，难度 {counts}")
    global_counts = {
        d: sum(1 for b in frame["blocks"] if b["difficulty"] == d)
        for d, _ in _DIFFICULTY_PER_CHAPTER
    }
    print(
        f"共 {frame['block_count']} 块（第 1/2/6 章 × 20；全局难度 {global_counts}，"
        f"计划冻结 easy/medium/hard = 24/24/12）"
    )


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
