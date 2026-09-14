"""gen_sample_cards.py：样卡真实生成演示脚本（零 DB、不落盘，结果打印到终端）。

用法示例（仓库根目录）：
  # 默认：第 1 章 10 张（基础 4 / 理解 4 / 应用 2），deepseek-flash
  conda run -n shanka-backend python scripts/gen_sample_cards.py

  # 只生成综合应用卡
  conda run -n shanka-backend python scripts/gen_sample_cards.py --count 5 --difficulty APPLICATION

  # 换章节 / 调比例 / 换模型
  conda run -n shanka-backend python scripts/gen_sample_cards.py \
    --count 10 --ratio 4:4:2 --chapter-prefix "第 2 章" --model deepseek-v4-pro

  # 带自定义提示词（验证语言/侧重类偏好的端到端遵守情况）
  conda run -n shanka-backend python scripts/gen_sample_cards.py \
    --chapter-prefix "第 3 章" --custom-requirements "生成不要有英文"

流程：解析样书 → 取目标章（默认"第 1 章"）页文本 → 粗规划（整章一次调用盘点主题清单，
planner-coarse v8）→ 精规划（主题打包成批调用展开为生成单元，planner v8）→ 每个学习
目标一次真实 Generator 调用（锚定单卡生成，generator v7）→ 终端打印卡片与汇总；粗规划
与 Generator 第一单元的双消息（system 资产原文 + user 动态信封）作为代表在调用前打印，
其余同构省略。

复用生产链路资产与逻辑（agent_evolution 版本化 Prompt/Schema、quota 三层配额、
planner 输出校验），不建任务不写库，无账本/幂等/评分。Key 安全遵循红线 4：
DEEPSEEK_API_KEY 仅从 .env 运行时读取、只在进程内使用，任何输出不出现明文。
"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# 脚本位于 scripts/，import 生产代码需把 main/ 注入 sys.path（路径基于 __file__，任意 cwd 可用）
_MAIN_DIR = Path(__file__).resolve().parents[1] / "main"
sys.path.insert(0, str(_MAIN_DIR))

import jsonschema
from pypdf import PdfReader

from app.config import Settings
from infra.llm.deepseek import DeepSeekClient, RetryableUpstreamError
from infra.llm.prompts import load_asset, load_schema_asset, safe_json_dumps
from services.generation.coarse_validator import validate_and_normalize_topics
from services.generation.cost import estimate_cost_by_kind
from services.generation.planner_validator import validate_and_truncate
from services.generation.quota import (
    allocate_group_quota,
    allocate_task_quota,
    expand_page_window,
    pack_topic_batches,
)
from services.pdf.parser import PageText, parse_pdf

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PDF = _REPO_ROOT / "res" / "AI-Agents-in-Depth-zh-CN.pdf"  # 样书只读引用，勿替换
_DEFAULT_ENV_FILE = _REPO_ROOT / ".env"
_DEFAULT_CHAPTER_PREFIX = "第 1 章"
_ATTEMPTS = 3  # 单次逻辑调用最多尝试次数（含输出非法重试；对齐生产预算口径）
_DIFFICULTY_LABEL = {"BASIC": "基础记忆", "UNDERSTANDING": "理解分析", "DEEP_QUESTION": "综合应用"}


def load_env_file(path: Path) -> dict[str, str]:
    """手写 .env 解析（KEY=VALUE，忽略注释/空行，去首尾空白与引号）。红线 4：仅进程内使用。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def find_chapter(pdf_path: Path, prefix: str) -> tuple[str, int, int]:
    """parse_pdf 顶层章节中按名称前缀匹配目标章；必须恰好一个（结构漂移即失败）。"""
    _sample, chapters = parse_pdf(pdf_path)
    matches = [ch for ch in chapters if ch["name"].startswith(prefix)]
    if len(matches) != 1:
        available = "；".join(ch["name"] for ch in chapters)
        raise SystemExit(
            f"目标章 {prefix!r} 应恰好 1 个，实际 {len(matches)} 个；可用章：{available}"
        )
    ch = matches[0]
    return ch["name"], int(ch["start_page"]), int(ch["end_page"])


def extract_chapter_pages(pdf_path: Path, start_page: int, end_page: int) -> list[PageText]:
    """只提取目标章闭区间 [start_page, end_page] 的页文本（页码 1-based；空页保留）。

    与 services.pdf.parser.extract_pages 同语义（extract_text() or ""），但按区间截取，
    避免全 PDF 逐页提取。
    """
    reader = PdfReader(str(pdf_path))
    pages: list[PageText] = []
    for page in reader.pages[start_page - 1 : end_page]:
        content = (page.extract_text() or "").strip()
        pages.append(PageText(page_number=start_page + len(pages), content=content))
    non_empty = [p for p in pages if p["content"]]
    if not non_empty:
        raise SystemExit(f"章节页 {start_page}-{end_page} 无可提取文本")
    return pages


def split_groups(pages: list[PageText], *, max_chars: int) -> list[list[PageText]]:
    """按连续页累计字符拆组（与 planning_executor._split_groups 同款规则：页序贪心累计，
    超预算开新组；单页超预算独立成组）。"""
    groups: list[list[PageText]] = []
    current: list[PageText] = []
    current_chars = 0
    for page in pages:
        char_count = len(page["content"])
        if char_count > max_chars:
            if current:
                groups.append(current)
                current = []
                current_chars = 0
            groups.append([page])
            continue
        if current and current_chars + char_count > max_chars:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(page)
        current_chars += char_count
    if current:
        groups.append(current)
    return groups


def coarse_prompts(
    chapter_name: str,
    pages: list[PageText],
    count: int,
    settings: Settings,
    custom_requirements: str | None,
) -> tuple[str, str]:
    """粗规划双消息组装（与 planning_executor._build_coarse_prompts 同款）：
    稳定 system（planner-coarse v7 + planner-coarse-output schema 原文）+ 动态 user。

    无 DB：chunk_id 用 "page:{page_number}" 自造（生产为 uuid5，planner 只作 opaque 引用）。
    演示脚本固定数量：主题区间退化为 [count, count]。
    """
    system_prompt = (
        f"{load_asset('prompts', 'planner_coarse')}\n\n<PLANNER_COARSE_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'planner_coarse_output')}\n</PLANNER_COARSE_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {
            "name": chapter_name,
            "start_page": pages[0]["page_number"],
            "end_page": pages[-1]["page_number"],
        },
        "coverage_mode": "EXTENSIVE",
        "topic_interval": {"min": count, "max": count},
        "limits": {
            "max_source_chunks_per_topic": settings.max_source_pages_per_unit,
            "max_source_chars_per_topic": settings.generator_max_input_chars,
        },
        "source_chunks": [
            {
                "chunk_id": f"page:{p['page_number']}",
                "page_number": p["page_number"],
                "content": p["content"],
            }
            for p in pages
        ],
        "custom_requirements": custom_requirements,
    }
    return system_prompt, f"<PLANNER_COARSE_INPUT>{safe_json_dumps(payload)}</PLANNER_COARSE_INPUT>"


def fine_prompts(
    chapter_name: str,
    topics: list[dict[str, Any]],
    pages: list[PageText],
    interval: dict[str, dict[str, int]],
    settings: Settings,
    custom_requirements: str | None,
) -> tuple[str, str]:
    """精规划双消息组装（与 planning_executor._build_fine_prompts 同款）：
    稳定 system（planner v8 + planner-output schema 原文）+ 动态 user（批主题+批页）。"""
    system_prompt = (
        f"{load_asset('prompts', 'planner')}\n\n<PLANNER_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'planner_output')}\n</PLANNER_OUTPUT_SCHEMA>"
    )
    payload = {
        "chapter": {"name": chapter_name},
        "coverage_mode": "EXTENSIVE",
        "topics": [
            {
                "topic_index": t["topic_index"],
                "title": t["title"],
                "coverage_tier": t["coverage_tier"],
                "source_chunk_ids": t["source_chunk_ids"],
            }
            for t in topics
        ],
        "difficulty_interval": interval,
        "limits": {
            "max_source_chunks_per_unit": settings.max_source_pages_per_unit,
            "max_source_chars_per_unit": settings.generator_max_input_chars,
        },
        "source_chunks": [
            {
                "chunk_id": f"page:{p['page_number']}",
                "page_number": p["page_number"],
                "content": p["content"],
            }
            for p in pages
        ],
        "custom_requirements": custom_requirements,
    }
    return system_prompt, f"<PLANNER_INPUT>{safe_json_dumps(payload)}</PLANNER_INPUT>"


def generator_prompts(
    unit: dict[str, Any], page_by_id: dict[str, PageText], custom_requirements: str | None
) -> tuple[str, str]:
    """Generator 双消息组装（与 batches._build_generator_prompts 同款三区块，V25-D-27）：
    稳定 system（generator prompt + generator-output schema 原文）+ 动态 user
    （<GENERATION_SPEC> + <SOURCE_MATERIAL> + <USER_REQUIREMENTS>）。
    """
    system_prompt = (
        f"{load_asset('prompts', 'generator')}\n\n<GENERATOR_OUTPUT_SCHEMA>\n"
        f"{load_asset('schemas', 'generator_output')}\n</GENERATOR_OUTPUT_SCHEMA>"
    )
    spec = {
        "learning_objective": unit["learning_objective"],
        "target_difficulty": unit["target_difficulty"],
        "card_type": unit["card_type"],
        "coverage_tier": unit.get("coverage_tier"),
    }
    source_material = [
        {"page_number": page_by_id[cid]["page_number"], "content": page_by_id[cid]["content"]}
        for cid in unit["source_chunk_ids"]
    ]
    user_prompt = (
        f"<GENERATION_SPEC>{safe_json_dumps(spec)}</GENERATION_SPEC>\n"
        f"<SOURCE_MATERIAL>{safe_json_dumps(source_material)}</SOURCE_MATERIAL>\n"
        f"<USER_REQUIREMENTS>{safe_json_dumps({'custom_requirements': custom_requirements})}</USER_REQUIREMENTS>"
    )
    return system_prompt, user_prompt


def chat_with_retry(
    client: DeepSeekClient, system_prompt: str, user_prompt: str, max_tokens: int, what: str
) -> dict[str, Any]:
    """逻辑调用 + 预算内重试：上游暂时失败（429/5xx/网络）与输出解析失败重试；401 Key 错误
    直接退出（fail fast，不浪费余额）。"""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            return client.chat(user_prompt, system_prompt=system_prompt, max_tokens=max_tokens)
        except RetryableUpstreamError as exc:
            if not exc.retryable:
                raise SystemExit(f"{what}: {exc.code.value}（Key 错误/不可恢复，退出）") from None
            print(f"    [{what}] 尝试 {attempt}/{_ATTEMPTS} 失败（{exc.code.value}），重试…")
            if attempt == _ATTEMPTS:
                raise SystemExit(f"{what}: {exc.code.value}（重试耗尽）") from None
            time.sleep(1.0)
    raise SystemExit(f"{what}: 重试耗尽")


def _accumulate_usage(total: dict[str, int], result: dict[str, Any]) -> None:
    """累加单次调用的 usage 到汇总（缺键按 0）。"""
    usage = result["usage"]
    total["prompt"] += int(usage.get("prompt_tokens") or 0)
    total["cache_hit"] += int(usage.get("prompt_cache_hit_tokens") or 0)
    total["cache_miss"] += int(usage.get("prompt_cache_miss_tokens") or 0)
    total["output"] += int(usage.get("completion_tokens") or 0)


_PREVIEW_CHARS = 120  # 打印 user prompt 时 source 内容的预览上限（全文省略，仅标识 + 片段）


def _brief_content(content: str) -> str:
    """source 内容预览：≤ 上限全显，超限截断并标注全文长度。"""
    if len(content) <= _PREVIEW_CHARS:
        return content
    return f"{content[:_PREVIEW_CHARS]}…［省略：全文 {len(content)} 字符］"


def _summarize_source_contents(payload: dict[str, Any]) -> dict[str, Any]:
    """user prompt 展示副本：source_chunks / source_material 的 content 只留标识 + 预览。

    只改打印副本，真实发送的 prompt 仍是完整原文（组装在 planner_prompts /
    generator_prompts，此处不回头改它）。
    """
    shown = dict(payload)
    for key in ("source_chunks", "source_material"):
        entries = shown.get(key)
        if not isinstance(entries, list):
            continue
        shown[key] = [
            {**entry, "content": _brief_content(entry["content"])}
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("content"), str)
        ]
    return shown


def print_prompts(tag: str, system_prompt: str, user_prompt: str, envelope: str) -> None:
    """打印一次逻辑调用的双消息：system 完整原文；user 信封完整结构但 source 原文省略。

    user prompt 按信封标签解析后重排为缩进 JSON 展示（只影响打印，不影响真实发送内容）。
    """
    print("=" * 72)
    print(f"[{tag}] SYSTEM PROMPT（含资产原文 + 输出 Schema 信封）")
    print("-" * 72)
    print(system_prompt)
    print("-" * 72)
    print(f"[{tag}] USER PROMPT（<{envelope}> 动态信封；source 原文省略，仅标识 + 预览）")
    print("-" * 72)
    try:
        inner = user_prompt.split(f"<{envelope}>", 1)[1].rsplit(f"</{envelope}>", 1)[0]
        payload = _summarize_source_contents(json.loads(inner))
        print(f"<{envelope}>{json.dumps(payload, ensure_ascii=False, indent=2)}</{envelope}>")
    except (ValueError, IndexError, TypeError):
        print(user_prompt)  # 信封不可解析（理论不可达）：原样打印兜底
    print("=" * 72)


def parse_json_result(result: dict[str, Any], what: str) -> dict[str, Any] | None:
    """chat 结果 content → JSON dict；非法返回 None（由调用方决定重试/跳过）。"""
    try:
        raw = json.loads(result["content"])
        if not isinstance(raw, dict):
            return None
        return raw
    except (ValueError, TypeError):
        print(f"    [{what}] 响应非 JSON，跳过")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="样卡真实生成演示：目标章 → Planner → Generator → 终端打印"
    )
    parser.add_argument("--count", type=int, default=10, help="目标卡数（默认 10）")
    parser.add_argument(
        "--ratio",
        type=str,
        default="4:4:2",
        help="难度配额比 BASIC:UNDERSTANDING:APPLICATION（默认 4:4:2）",
    )
    parser.add_argument("--pdf", type=Path, default=_DEFAULT_PDF, help="样书 PDF（只读引用）")
    parser.add_argument(
        "--chapter-prefix",
        type=str,
        default=_DEFAULT_CHAPTER_PREFIX,
        help="章名匹配前缀（默认 '第 1 章'）",
    )
    parser.add_argument("--env-file", type=Path, default=_DEFAULT_ENV_FILE, help=".env 路径")
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="覆盖模型名（默认 Settings 值 deepseek-flash；可试 deepseek-v4-pro）",
    )
    parser.add_argument(
        "--difficulty",
        choices=("BASIC", "UNDERSTANDING", "APPLICATION"),
        default=None,
        help="只生成指定难度（配额全给该难度；与显式 --ratio 互斥）",
    )
    parser.add_argument(
        "--custom-requirements",
        type=str,
        default=None,
        help="自定义提示词（透传三个阶段的 custom_requirements，验证遵守情况）",
    )
    parser.add_argument(
        "--dump",
        type=Path,
        default=None,
        help="导出 JSON（items 含卡与锚定 + 全章页原文），供离线评分/复核使用",
    )
    args = parser.parse_args()
    if args.count < 1:
        raise SystemExit("--count 必须 >= 1")
    parts = args.ratio.split(":")
    if len(parts) != 3 or any(not p.isdigit() or int(p) < 0 for p in parts):
        raise SystemExit("--ratio 格式非法，应为 B:U:A（如 4:4:2）")
    ratio = tuple(int(p) for p in parts)
    if args.difficulty and args.ratio != "4:4:2":
        raise SystemExit("--difficulty 与显式 --ratio 互斥，请二选一")

    api_key = load_env_file(args.env_file).get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(f"{args.env_file} 缺少 DEEPSEEK_API_KEY（仅运行时读取，不落盘不打印）")

    settings = Settings()
    if args.model:
        settings = settings.model_copy(update={"deepseek_model": args.model})
    chapter_name, start_page, end_page = find_chapter(args.pdf, args.chapter_prefix)
    pages = extract_chapter_pages(args.pdf, start_page, end_page)
    if args.difficulty == "APPLICATION":  # 旧难度名兼容（V2.5 起枚举为 DEEP_QUESTION）
        args.difficulty = "DEEP_QUESTION"
    if args.difficulty:
        task_quota = {args.difficulty: args.count}
        for d in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION"):
            task_quota.setdefault(d, 0)
    else:
        task_quota = allocate_task_quota(args.count, *[r / sum(ratio) for r in ratio])

    print("=" * 72)
    print(f"样卡真实生成演示：目标 {args.count} 张（配额 {task_quota}）")
    print(f"章节：{chapter_name}（页 {start_page}-{end_page}，{len(pages)} 页）")
    print(
        f"模型：{settings.deepseek_model}（thinking={'on' if settings.deepseek_thinking else 'off'}）"
    )
    if args.custom_requirements:
        print(f"自定义提示词：{args.custom_requirements}")
    print("=" * 72)

    page_by_id = {f"page:{p['page_number']}": p for p in pages}
    client = DeepSeekClient(settings, api_key=api_key)
    total_usage = {"prompt": 0, "cache_hit": 0, "cache_miss": 0, "output": 0}
    calls = 0
    started = time.monotonic()
    merged: list[dict[str, Any]] = []
    try:
        # ---- Coarse Planner：整章一次盘点主题（超粗规划上限按连续页分段）----
        segments = split_groups(pages, max_chars=settings.planner_coarse_max_input_chars)
        topics: list[dict[str, Any]] = []
        for si, seg in enumerate(segments, start=1):
            sys_prompt, user_prompt = coarse_prompts(
                chapter_name, seg, args.count, settings, args.custom_requirements
            )
            if si == 1:
                print_prompts(
                    f"Coarse Planner 段{si}/{len(segments)}（代表，后续同构省略）",
                    sys_prompt,
                    user_prompt,
                    "PLANNER_COARSE_INPUT",
                )
            result = chat_with_retry(
                client,
                sys_prompt,
                user_prompt,
                max_tokens=settings.planner_coarse_max_output_tokens,
                what=f"Coarse Planner 段{si}/{len(segments)}",
            )
            calls += 1
            _accumulate_usage(total_usage, result)
            raw = parse_json_result(result, f"Coarse Planner 段{si}")
            if raw is None:
                continue
            try:
                seg_topics = validate_and_normalize_topics(
                    raw,
                    coverage_mode="EXTENSIVE",
                    topic_interval=(args.count, args.count),
                    allowed_page_ids={f"page:{pg['page_number']}" for pg in seg},
                    max_chunks_per_topic=settings.max_source_pages_per_unit,
                    max_chars_per_topic=settings.generator_max_input_chars,
                    page_chars={f"page:{pg['page_number']}": len(pg["content"]) for pg in seg},
                )
            except Exception as exc:  # noqa: BLE001 —— 输出非法：脚本不重试，跳过该段
                print(f"    [Coarse 段{si}] 输出校验失败（{type(exc).__name__}），跳过该段")
                continue
            topics.extend(seg_topics)
        for i, topic in enumerate(topics, start=1):
            topic["topic_index"] = i
        if not topics:
            raise SystemExit("Coarse Planner 未产出任何有效主题，退出")
        print(f"[粗规划] {len(topics)} 主题 → 精规划展开")

        # ---- Fine Planner：主题打包成批展开为生成单元 ----
        page_chars_all = {f"page:{p['page_number']}": len(p["content"]) for p in pages}
        position_of = {f"page:{p['page_number']}": i for i, p in enumerate(pages)}
        packed = pack_topic_batches(
            topics,
            page_chars_all,
            max_chars=settings.planner_max_input_chars,
            max_topics=settings.planner_fine_topics_per_call,
        )
        for bi, batch in enumerate(packed, start=1):
            batch_topics = [topics[i] for i in batch]
            window = expand_page_window(
                {position_of[cid] for t in batch_topics for cid in t["source_chunk_ids"]},
                len(pages),
                margin=settings.planner_fine_page_margin,
            )
            batch_pages = [pages[pos] for pos in sorted(window)]
            batch_chars = sum(len(p["content"]) for p in batch_pages)
            batch_quota = allocate_group_quota(task_quota, [batch_chars])[0]
            interval = {d: {"min": 0, "max": q} for d, q in batch_quota.items()}
            sys_prompt, user_prompt = fine_prompts(
                chapter_name,
                batch_topics,
                batch_pages,
                interval,
                settings,
                args.custom_requirements,
            )
            result = chat_with_retry(
                client,
                sys_prompt,
                user_prompt,
                max_tokens=settings.planner_max_output_tokens,
                what=f"Fine Planner 批{bi}/{len(packed)}",
            )
            calls += 1
            _accumulate_usage(total_usage, result)
            raw = parse_json_result(result, f"Fine Planner 批{bi}")
            if raw is None:
                continue
            try:
                units = validate_and_truncate(
                    raw,
                    topics=batch_topics,
                    allowed_page_ids={f"page:{pg['page_number']}" for pg in batch_pages},
                    interval=interval,
                    max_pages_per_unit=settings.max_source_pages_per_unit,
                    max_chars_per_unit=settings.generator_max_input_chars,
                    page_chars={
                        f"page:{pg['page_number']}": len(pg["content"]) for pg in batch_pages
                    },
                )
            except Exception as exc:  # noqa: BLE001 —— 输出非法：脚本不重试，跳过该批
                print(f"    [Fine 批{bi}] 输出校验失败（{type(exc).__name__}），跳过该批")
                continue
            pages_span = f"{batch_pages[0]['page_number']}-{batch_pages[-1]['page_number']}"
            print(
                f"[精规划] 批 {bi}/{len(packed)}（页 {pages_span}，{batch_chars} 字符，"
                f"{len(batch_topics)} 主题）→ {len(units)} 单元"
            )
            merged.extend(units)
        # 跨组去重（生产 _merge_units 同款指纹：目标+难度+卡型+来源页集合）
        seen: set[tuple[Any, ...]] = set()
        units: list[dict[str, Any]] = []
        for unit in merged:
            key = (
                unit["learning_objective"],
                unit["target_difficulty"],
                unit["card_type"],
                tuple(sorted(unit["source_chunk_ids"])),
            )
            if key in seen:
                continue
            seen.add(key)
            units.append(unit)
        if not units:
            raise SystemExit("Planner 未产出任何有效单元，退出")

        # ---- Generator：每单元一次调用，锚定单卡生成 ----
        generated: list[tuple[dict[str, Any], dict[str, Any]]] = []  # (unit, card)
        generator_schema = load_schema_asset("generator_output")
        for i, unit in enumerate(units, start=1):
            sys_prompt, user_prompt = generator_prompts(unit, page_by_id, args.custom_requirements)
            if i == 1:
                print_prompts(
                    f"Generator {i}/{len(units)}（代表，后续单元同构省略）",
                    sys_prompt,
                    user_prompt,
                    "GENERATION_SPEC",
                )
            result = chat_with_retry(
                client,
                sys_prompt,
                user_prompt,
                max_tokens=settings.generator_max_output_tokens,
                what=f"Generator {i}/{len(units)}",
            )
            calls += 1
            usage = result["usage"]
            total_usage["prompt"] += int(usage.get("prompt_tokens") or 0)
            total_usage["cache_hit"] += int(usage.get("prompt_cache_hit_tokens") or 0)
            total_usage["cache_miss"] += int(usage.get("prompt_cache_miss_tokens") or 0)
            total_usage["output"] += int(usage.get("completion_tokens") or 0)
            raw = parse_json_result(result, f"Generator {i}")
            if raw is None:
                continue
            if list(jsonschema.Draft202012Validator(generator_schema).iter_errors(raw)):
                print(f"    [Generator {i}] 输出 Schema 违约，跳过")
                continue
            if raw["cards"]:
                generated.append((unit, raw["cards"][0]))
                print(
                    f"[生成] {i:02d}/{len(units)} "
                    f"{unit['target_difficulty']} · {unit['card_type']} · 1 卡"
                )
            else:
                print(f"[生成] {i:02d}/{len(units)} 证据不足弃权（空 cards）")
    finally:
        client.close()
    wall = time.monotonic() - started

    if args.dump and generated:
        dump = {
            "chapter": chapter_name,
            "custom_requirements": args.custom_requirements,
            "pages": [
                {
                    "chunk_id": f"page:{p['page_number']}",
                    "page_number": p["page_number"],
                    "content": p["content"],
                }
                for p in pages
            ],
            "items": [
                {
                    "generation_item_id": f"item-{i:03d}",
                    "learning_objective": unit["learning_objective"],
                    "target_difficulty": unit["target_difficulty"],
                    "card_type": unit["card_type"],
                    "card": {
                        "card_type": card["type"],
                        "question": card.get("question"),
                        "answer": card.get("answer"),
                        "statement": card.get("statement"),
                        "explanation": card.get("explanation"),
                        "answer_boolean": card.get("answer_boolean"),
                    },
                    "source_chunk_ids": list(unit["source_chunk_ids"]),
                }
                for i, (unit, card) in enumerate(generated, start=1)
            ],
        }
        args.dump.write_text(
            json.dumps(dump, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        print(f"已导出 {len(generated)} 卡 → {args.dump}")

    # ---- 终端打印 ----
    print("=" * 72)
    for i, (unit, card) in enumerate(generated, start=1):
        difficulty = _DIFFICULTY_LABEL.get(unit["target_difficulty"], unit["target_difficulty"])
        print(f"[{i:02d}/{len(generated)}] {difficulty} · {card['type']}")
        print(f"  目标: {unit['learning_objective']}")
        if card["type"] == "QUESTION":
            print(f"  正面: {card['question']}")
            print(f"  背面: {card['answer']}")
        else:
            print(f"  命题: {card['statement']}")
            print(f"  判断: {'正确' if card['answer_boolean'] else '错误'}")
            print(f"  解释: {card['explanation']}")
        print("-" * 72)
    effective_date = datetime.now(UTC).date().isoformat()
    cost = estimate_cost_by_kind(
        total_usage["cache_hit"],
        total_usage["cache_miss"],
        total_usage["output"],
        effective_date=effective_date,
    )
    print(f"汇总: 卡 {len(generated)}/{len(units)} 单元 · 调用 {calls} 次 · 耗时 {wall:.1f}s")
    print(
        f"tokens: prompt={total_usage['prompt']} cache_hit={total_usage['cache_hit']} "
        f"cache_miss={total_usage['cache_miss']} output={total_usage['output']}"
    )
    if args.model and args.model != "deepseek-flash":
        print(f"注意: 价格表为 deepseek-flash 价目，{args.model} 实际计费以官方账单为准")
    print(f"估算成本（{effective_date} 价目）: ¥{cost['total']}")


if __name__ == "__main__":
    main()
