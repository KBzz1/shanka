"""quota.py：任务预算与三层配额（纯函数，无 DB 依赖；spec 3.5 难度配额算法）。

三层确定性分配全部代码计算，固定顺序（BASIC < UNDERSTANDING < APPLICATION、
章序、组序）消除随机性：任务总配额 → 章配额 → 子配额，各层均用最大余数法
（largest remainder）取整到总和守恒。
"""

_DENSITY = {"COMPACT": 1, "BALANCED": 2, "EXTENSIVE": 3}
_BASE_CHUNKS = 3  # 每章基础分块数（确定性；真实文本分块 V5A 接入）


def task_unit_budget(chapter_count: int, quantity_tendency: str) -> int:
    """任务总预算（5.4.1 口径）：章节数 × 3 × 密度系数，未知密度回落 BALANCED。"""
    density = _DENSITY.get(quantity_tendency, 2)
    return chapter_count * _BASE_CHUNKS * density


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
    ratio_application: float,
) -> dict[str, int]:
    """任务三层配额：总预算 × 难度占比，最大余数法取整到总和 = 总预算。"""
    return largest_remainder(
        [
            total_budget * ratio_basic,
            total_budget * ratio_understanding,
            total_budget * ratio_application,
        ],
        total_budget,
        ["BASIC", "UNDERSTANDING", "APPLICATION"],
    )


def allocate_chapter_quota(task_quota: dict[str, int], chapter_count: int) -> list[dict[str, int]]:
    """章配额：每难度按章均分 + 最大余数法按章序（并列余数补最前章）。"""
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
    """子配额：每难度按各分组 char_count 占比，最大余数法按组序；字符和为 0 时均分。"""
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
