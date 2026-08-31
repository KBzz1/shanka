"""quota.py：任务预算与配额（纯函数，无 DB 依赖；spec 3.5 + 密度制 V25-D-25）。

V2.5.1 密度制：目标卡数 = 章节字符数 / 10000 × 每万字密度锚点
（{COMPACT:6, BALANCED:12, EXTENSIVE:20}，Settings 可调）。下发给 Planner 的是
目标区间而不是硬上限——区间内按内容取舍（薄内容不注水、富内容不偷工）；区间
上限仍是 Validator 确定性截断的硬上限，超上限不重试。

三层确定性分配（难度 < 章 < 组）全部代码计算，固定顺序（BASIC < UNDERSTANDING <
DEEP_QUESTION、章序、组序）消除随机性，各层均用最大余数法取整到总和守恒。
"""

import math

_DENSITY = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}
_BASE_CHUNKS = 3  # 旧"章数×3×密度"估算口径保留（创建期快速守卫）

# 每万字目标卡数锚点（V25-D-25：4.4 万字标准章 ≈ 26/53/88，对齐用户预期 25/50/80）
DEFAULT_CARDS_PER_10K: dict[str, float] = {
    "COMPACT": 6.0,
    "BALANCED": 12.0,
    "EXTENSIVE": 20.0,
}

_INTERVAL_FLOOR_RATIO = 0.6
_INTERVAL_CEIL_RATIO = 1.2
_INTERVAL_MIN_FLOOR = 3

Interval = dict[str, dict[str, int]]  # {"BASIC": {"min": x, "max": y}, ...}


def task_unit_budget(chapter_count: int, coverage_mode: str) -> int:
    """旧口径快速估算：章节数 × 3 × 密度系数，未知密度回落 BALANCED。

    仅用于创建期粗估守卫（无文本字符时的下界估算）；规划期真实预算走密度制。
    """
    density = _DENSITY.get(coverage_mode, 2)
    return chapter_count * _BASE_CHUNKS * density


def estimate_task_units(total_char_count: int, coverage_mode: str) -> int:
    """密度制任务单元估算（创建期守卫口径）：区间上限合计 ≈ ⌈字符/万 × 密度 × 1.2⌉。"""
    density = DEFAULT_CARDS_PER_10K.get(coverage_mode, DEFAULT_CARDS_PER_10K["BALANCED"])
    return math.ceil(total_char_count / 10_000 * density * _INTERVAL_CEIL_RATIO)


def chapter_target_cards(
    char_count: int,
    coverage_mode: str,
    cards_per_10k: dict[str, float] | None = None,
) -> float:
    """单章目标卡数（密度制核心公式）：字符数 / 10000 × 密度锚点，未知模式回落 BALANCED。"""
    anchors = cards_per_10k or DEFAULT_CARDS_PER_10K
    density = anchors.get(coverage_mode, anchors["BALANCED"])
    return char_count / 10_000 * density


def target_interval(target: float) -> tuple[int, int]:
    """目标卡数 → Planner 区间 [下界, 上界]。

    下界 = max(3, ⌊0.6t⌋) 再被上界封顶（薄内容如实给小区间，不人为垫高注水）；
    上界 = ⌈1.2t⌉。target=0 → (0, 0)（空内容不产出）。
    """
    import math

    if target <= 0:
        return (0, 0)
    upper = math.ceil(target * _INTERVAL_CEIL_RATIO)
    lower = max(_INTERVAL_MIN_FLOOR, math.floor(target * _INTERVAL_FLOOR_RATIO))
    return (min(lower, upper), upper)


def interval_for_chapter(
    char_count: int,
    coverage_mode: str,
    cards_per_10k: dict[str, float] | None = None,
) -> tuple[int, int]:
    """便捷组合：章节字符数 → 该章目标区间。"""
    return target_interval(chapter_target_cards(char_count, coverage_mode, cards_per_10k))


def difficulty_interval(
    chapter_interval: tuple[int, int],
    ratio_basic: float,
    ratio_understanding: float,
    ratio_deep_question: float,
) -> Interval:
    """章区间按难度占比拆分：min/max 各自最大余数法拆分，总和守恒（不突破章上限）。

    占比为 0 或章区间上界为 0 → 该难度 (0, 0)（0 保持"禁止输出"语义）；
    极小占比拆得上界 0 但章级有量 → 至少留 1，避免取整吞掉难度。
    """
    total_min, total_max = chapter_interval
    if total_max <= 0:
        return {d: {"min": 0, "max": 0} for d in ("BASIC", "UNDERSTANDING", "DEEP_QUESTION")}
    ratios = {
        "BASIC": max(ratio_basic, 0.0),
        "UNDERSTANDING": max(ratio_understanding, 0.0),
        "DEEP_QUESTION": max(ratio_deep_question, 0.0),
    }
    labels = ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"]
    split: Interval = {}
    for bound in ("min", "max"):
        total = total_max if bound == "max" else total_min
        shares = largest_remainder(
            [ratios[d] * total for d in labels],
            total,
            labels,
        )
        for d in labels:
            split.setdefault(d, {})[bound] = shares[d]
    for d in labels:
        if ratios[d] <= 0:
            split[d] = {"min": 0, "max": 0}
        elif split[d]["max"] == 0:
            split[d] = {"min": 0, "max": 1}
        else:
            split[d]["min"] = min(split[d]["min"], split[d]["max"])
    return split


def allocate_chapter_intervals(
    chapter_char_counts: list[int],
    coverage_mode: str,
    ratio_basic: float,
    ratio_understanding: float,
    ratio_deep_question: float,
    cards_per_10k: dict[str, float] | None = None,
) -> list[Interval]:
    """每章的难度区间：字符数 → 章区间 → 按难度占比拆分。"""
    return [
        difficulty_interval(
            interval_for_chapter(chars, coverage_mode, cards_per_10k),
            ratio_basic,
            ratio_understanding,
            ratio_deep_question,
        )
        for chars in chapter_char_counts
    ]


def allocate_group_interval(
    chapter_interval: Interval, group_char_counts: list[int]
) -> list[Interval]:
    """章内各组区间：每难度按组字符占比做最大余数法拆分，min/max 各自总和守恒。

    字符和为 0 时均分；单组章节原样返回。
    """
    group_count = len(group_char_counts)
    if group_count <= 1:
        return [dict(chapter_interval) for _ in range(max(group_count, 0))]
    total_chars = sum(group_char_counts)
    group_labels = [str(i) for i in range(group_count)]
    per_group: list[Interval] = [{} for _ in range(group_count)]
    for difficulty, bounds in chapter_interval.items():
        for bound in ("min", "max"):
            total = bounds[bound]
            if total_chars == 0:
                amounts = [total / group_count for _ in range(group_count)]
            else:
                amounts = [total * c / total_chars for c in group_char_counts]
            shares = largest_remainder(amounts, total, group_labels)
            for i in range(group_count):
                per_group[i].setdefault(difficulty, {})[bound] = shares[group_labels[i]]
    return per_group


def largest_remainder(amounts: list[float], total: int, order: list[str]) -> dict[str, int]:
    """最大余数法：整数部分 + 余数降序补 1（并列按 order 顺序），总和恒等于 total。

    返回 `{label: count}`；amounts 与 order 一一对应，labels 按 order 原序输出。
    """
    floors = [int(a) for a in amounts]
    result = {label: count for label, count in zip(order, floors)}
    deficit = total - sum(floors)
    by_remainder = sorted(
        range(len(amounts)),
        key=lambda i: (-(amounts[i] - floors[i]), i),
    )
    for i in by_remainder[:deficit]:
        result[order[i]] += 1
    return result


def allocate_task_quota(
    total_budget: int,
    ratio_basic: float,
    ratio_understanding: float,
    ratio_deep_question: float,
) -> dict[str, int]:
    """任务三层配额（旧口径，gen_sample_cards 演示脚本与历史测试仍用）：总预算 × 难度占比。"""
    return largest_remainder(
        [
            total_budget * ratio_basic,
            total_budget * ratio_understanding,
            total_budget * ratio_deep_question,
        ],
        total_budget,
        ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"],
    )


def allocate_chapter_quota(task_quota: dict[str, int], chapter_count: int) -> list[dict[str, int]]:
    """章配额（旧口径）：每难度按章均分 + 最大余数法按章序（并列余数补最前章）。"""
    chapter_labels = [str(i) for i in range(chapter_count)]
    per_chapter: list[dict[str, int]] = [{} for _ in range(chapter_count)]
    for difficulty, quota in task_quota.items():
        amounts = [quota / chapter_count for _ in range(chapter_count)]
        per_chapter_counts = largest_remainder(amounts, quota, chapter_labels)
        for i in range(chapter_count):
            per_chapter[i][difficulty] = per_chapter_counts[chapter_labels[i]]
    return per_chapter


def allocate_group_quota(
    chapter_quota: dict[str, int], group_char_counts: list[int]
) -> list[dict[str, int]]:
    """子配额（旧口径）：每难度按各分组 char_count 占比，最大余数法按组序；字符和为 0 时均分。"""
    group_count = len(group_char_counts)
    group_labels = [str(i) for i in range(group_count)]
    total_chars = sum(group_char_counts)
    per_group: list[dict[str, int]] = [{} for _ in range(group_count)]
    for difficulty, quota in chapter_quota.items():
        if total_chars == 0:
            amounts = [quota / group_count for _ in range(group_count)]
        else:
            amounts = [quota * c / total_chars for c in group_char_counts]
        per_group_counts = largest_remainder(amounts, quota, group_labels)
        for i in range(group_count):
            per_group[i][difficulty] = per_group_counts[group_labels[i]]
    return per_group
