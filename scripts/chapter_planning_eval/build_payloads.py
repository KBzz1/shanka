"""chapter_planning_eval.build_payloads：零 API 构建章节规划评测输入。

对指定 PDF（默认样书）产出 ``run/<书名>/``：

- ``ground_truth.json``：``parse_pdf`` 的 outline 章节（页级区间）——书自带目录即
  ground truth；
- ``pages.jsonl``：``extract_pages`` 全页文本（每行 {"page_number","content"}）；
- ``segments/seg_N_system.txt`` / ``seg_N_user.txt``：与生产**逐字节一致**的分段消息
  （生产 ``split_segments`` + ``build_segment_prompts``）；
- ``meta.json``：settings 快照（保证可复现）。

正文切块（pages.jsonl/segments）可由本脚本 + 原 PDF 重建，run/ 不入 Git。
用法：``conda run -n shanka-backend python scripts/chapter_planning_eval/build_payloads.py [--pdf PATH]``
"""

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "main"))

from app.config import Settings
from services.chapters.planner import (
    build_segment_prompts,
    split_segments,
)
from services.pdf.parser import extract_pages, parse_pdf

SAMPLE = _REPO / "res" / "AI-Agents-in-Depth-zh-CN.pdf"
OUT = Path(__file__).resolve().parent / "run"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pdf", type=Path, default=SAMPLE, help="评测用 PDF（默认样书）"
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        default=None,
        help="手工 GT JSON（无 outline 书的评测路径：章节结构肉眼可见时人工标注，"
        "格式同 parse_pdf 产出的 ChapterInfo 列表）；缺省从 PDF outline 提取",
    )
    args = parser.parse_args()

    pdf_path: Path = args.pdf
    if not pdf_path.exists():
        print(f"PDF 不存在: {pdf_path}")
        return 1
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    if args.ground_truth is not None:
        gt_chapters = json.loads(args.ground_truth.read_text(encoding="utf-8"))
        source = "manual"
    else:
        _sample, parsed = parse_pdf(pdf_path)  # 样书/参评书须有 outline（GT 来源）
        if parsed is None:
            print(
                "该 PDF 无 outline 目录；若章节结构肉眼可见，用 --ground-truth 提供手工标注"
            )
            return 1
        gt_chapters = parsed
        source = "outline"
    pages = extract_pages(pdf_path)

    book = pdf_path.stem
    book_dir = OUT / book
    (book_dir / "segments").mkdir(parents=True, exist_ok=True)

    (book_dir / "ground_truth.json").write_text(
        json.dumps(gt_chapters, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with (book_dir / "pages.jsonl").open("w", encoding="utf-8") as f:
        for page in pages:
            f.write(json.dumps(page, ensure_ascii=False) + "\n")

    # 与生产同款块形状（planner 只消费 page_number/content/char_count/sha）
    from infra.db.models import TextChunk

    chunks = [
        TextChunk(
            chunk_id=f"p{page['page_number']}",
            material_id="eval",
            chunk_seq=page["page_number"],
            page_number=page["page_number"],
            char_count=len(page["content"]),
            content=page["content"],
            content_sha256="eval",
            created_at="eval",
        )
        for page in pages
    ]
    segments = split_segments(chunks, max_chars=settings.ai_chapter_max_input_chars)
    first_page, last_page = chunks[0].page_number, chunks[-1].page_number
    for index, segment in enumerate(segments):
        system_prompt, user_prompt = build_segment_prompts(
            material_name=book,
            segment=segment,
            first_page=first_page,
            last_page=last_page,
            settings=settings,
        )
        (book_dir / "segments" / f"seg_{index}_system.txt").write_text(
            system_prompt, encoding="utf-8"
        )
        (book_dir / "segments" / f"seg_{index}_user.txt").write_text(
            user_prompt, encoding="utf-8"
        )

    (book_dir / "meta.json").write_text(
        json.dumps(
            {
                "pdf": str(pdf_path),
                "model": settings.deepseek_model,
                "ground_truth_source": source,
                "ai_chapter_max_input_chars": settings.ai_chapter_max_input_chars,
                "ai_chapter_max_segments": settings.ai_chapter_max_segments,
                "ai_chapter_max_boundaries_per_segment": settings.ai_chapter_max_boundaries_per_segment,
                "ai_chapter_max_output_tokens": settings.ai_chapter_max_output_tokens,
                "segment_count": len(segments),
                "total_pages": len(pages),
                "gt_chapter_count": len(gt_chapters),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"{book}: {len(pages)} 页 / GT {len(gt_chapters)} 章 / {len(segments)} 段消息 → {book_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
