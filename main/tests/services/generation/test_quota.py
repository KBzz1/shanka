"""quota.py 纯函数：任务预算与三层配额（spec 3.5 难度配额算法）。"""

from services.generation.quota import (
    allocate_chapter_quota,
    allocate_group_quota,
    allocate_task_quota,
    largest_remainder,
    task_unit_budget,
)


def test_budget_keeps_v4_caliber() -> None:
    assert task_unit_budget(2, "COMPACT") == 6
    assert task_unit_budget(2, "BALANCED") == 12
    assert task_unit_budget(2, "EXTENSIVE") == 18
    assert task_unit_budget(2, "WEIRD") == 12  # 未知回落 BALANCED


def test_task_quota_40_40_20_gives_3_2_1() -> None:
    assert allocate_task_quota(6, 0.4, 0.4, 0.2) == {
        "BASIC": 3,
        "UNDERSTANDING": 2,
        "APPLICATION": 1,
    }


def test_largest_remainder_total_preserved() -> None:
    out = largest_remainder([2.4, 2.4, 1.2], 6, ["BASIC", "UNDERSTANDING", "APPLICATION"])
    assert sum(out.values()) == 6


def test_chapter_quota_distributes_evenly() -> None:
    q = allocate_chapter_quota({"BASIC": 3, "UNDERSTANDING": 2, "APPLICATION": 1}, 2)
    assert sum(ch["BASIC"] for ch in q) == 3
    assert len(q) == 2


def test_group_quota_by_char_share() -> None:
    g = allocate_group_quota({"BASIC": 3}, [2000, 4000])
    assert sum(x["BASIC"] for x in g) == 3
    assert g[0]["BASIC"] == 1 and g[1]["BASIC"] == 2  # 1:2 占比
