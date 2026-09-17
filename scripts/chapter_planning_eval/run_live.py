"""chapter_planning_eval.run_live：真实调用 DeepSeek 逐段生成章节规划回复并留档。

读取仓库根 ``.env`` 的 ``DEEPSEEK_API_KEY``（``--env-file`` 可覆盖；红线 4：Key 仅
进程内使用，任何输出/留档/报告不出现明文）。每段一次生产 ``DeepSeekClient.chat``
（system/user = ``build_payloads.py`` 产出的消息文件），回复留档
``run/<书名>/replies/seg_N.json``（content/usage/http_status/duration_ms/model）——
``make_report.py`` 只消费留档文件，可离线反复重算不重复付费。

中断可续：已存在的 seg_N.json 跳过（``--force`` 重跑全部）。
用法：``conda run -n shanka-backend python scripts/chapter_planning_eval/run_live.py [--book NAME] [--env-file PATH] [--force]``
"""

import argparse
import json
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "main"))

from app.config import Settings
from infra.llm.deepseek import DeepSeekClient

OUT = Path(__file__).resolve().parent / "run"


def _load_key(env_file: Path) -> str:
    if not env_file.exists():
        raise SystemExit(f"env 文件不存在: {env_file}")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("DEEPSEEK_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise SystemExit(f"{env_file} 中未找到 DEEPSEEK_API_KEY")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--book", default=None, help="书名（run/ 下的目录名；缺省取唯一一个）"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=_REPO / ".env",
        help="DEEPSEEK_API_KEY 来源（默认仓库根 .env）",
    )
    parser.add_argument("--force", action="store_true", help="已留档的段也重跑")
    args = parser.parse_args()

    book_dir = OUT / args.book if args.book else None
    if book_dir is None or not book_dir.exists():
        candidates = (
            sorted(p for p in OUT.iterdir() if p.is_dir()) if OUT.exists() else []
        )
        if len(candidates) == 1:
            book_dir = candidates[0]
        else:
            print(
                f"无法定位书目录（--book 指定或 run/ 下唯一目录）；现有: {[p.name for p in candidates]}"
            )
            return 1
    segments_dir = book_dir / "segments"
    system_files = sorted(segments_dir.glob("seg_*_system.txt"))
    if not system_files:
        print(f"{segments_dir} 无分段消息；先跑 build_payloads.py")
        return 1

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    api_key = _load_key(args.env_file)
    replies_dir = book_dir / "replies"
    replies_dir.mkdir(exist_ok=True)

    client = DeepSeekClient(settings, api_key=api_key)
    total_ms = 0
    try:
        for system_file in system_files:
            seg_index = system_file.name.split("_")[1]
            reply_file = replies_dir / f"seg_{seg_index}.json"
            if reply_file.exists() and not args.force:
                print(f"seg_{seg_index}: 已留档，跳过（--force 重跑）")
                continue
            user_prompt = (segments_dir / f"seg_{seg_index}_user.txt").read_text(
                encoding="utf-8"
            )
            system_prompt = system_file.read_text(encoding="utf-8")
            start = time.monotonic()
            result = client.chat(
                user_prompt,
                system_prompt=system_prompt,
                max_tokens=settings.ai_chapter_max_output_tokens,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            total_ms += duration_ms
            reply_file.write_text(
                json.dumps(
                    {
                        "content": result["content"],
                        "usage": result["usage"],
                        "http_status": result["http_status"],
                        "duration_ms": result["duration_ms"],
                        "model": result.get("model", settings.deepseek_model),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                f"seg_{seg_index}: ok {result['http_status']} {duration_ms}ms "
                f"cache_hit={result['usage'].get('prompt_cache_hit_tokens')}"
            )
    finally:
        client.close()
    print(
        f"完成：{book_dir.name} 回复留档于 {replies_dir}（本进程新调用 {total_ms}ms）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
