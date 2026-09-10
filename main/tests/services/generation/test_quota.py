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
        "DEEP_QUESTION": 1,
    }


def test_largest_remainder_total_preserved() -> None:
    out = largest_remainder([2.4, 2.4, 1.2], 6, ["BASIC", "UNDERSTANDING", "DEEP_QUESTION"])
    assert sum(out.values()) == 6


def test_chapter_quota_distributes_evenly() -> None:
    q = allocate_chapter_quota({"BASIC": 3, "UNDERSTANDING": 2, "DEEP_QUESTION": 1}, 2)
    assert sum(ch["BASIC"] for ch in q) == 3
    assert len(q) == 2


def test_group_quota_by_char_share() -> None:
    g = allocate_group_quota({"BASIC": 3}, [2000, 4000])
    assert sum(x["BASIC"] for x in g) == 3
    assert g[0]["BASIC"] == 1 and g[1]["BASIC"] == 2  # 1:2 占比


def test_pack_topic_batches_char_and_topic_caps() -> None:
    """主题打包：批字符按批内页并集计、主题数上限触发开新批、共享页不重复计。"""
    from services.generation.quota import pack_topic_batches

    topics: list[dict[str, object]] = [
        {"source_chunk_ids": ["p1", "p2"]},
        {"source_chunk_ids": ["p2", "p3"]},  # 与上一主题共享 p2 → 并集增量小
        {"source_chunk_ids": ["p4"]},
        {"source_chunk_ids": ["p5"]},
    ]
    page_chars = {"p1": 100, "p2": 100, "p3": 100, "p4": 100, "p5": 100}
    # 字符上限 350：批0 = t0(p1,p2=200) + t1(并集+p3=300) → 再加 t2 超限开新批
    batches = pack_topic_batches(topics, page_chars, max_chars=350, max_topics=8)
    assert batches == [[0, 1], [2, 3]]
    # 主题数上限 1：每主题一批
    batches_one = pack_topic_batches(topics, page_chars, max_chars=10_000, max_topics=1)
    assert batches_one == [[0], [1], [2], [3]]


def test_pack_topic_batches_empty() -> None:
    from services.generation.quota import pack_topic_batches

    assert pack_topic_batches([], {"p1": 1}, max_chars=100, max_topics=8) == []


def test_expand_page_window_margin() -> None:
    """页窗口：声明页 ∪ ±margin，夹在章节页范围内。"""
    from services.generation.quota import expand_page_window

    assert expand_page_window({0}, total_pages=5, margin=1) == {0, 1}
    assert expand_page_window({4}, total_pages=5, margin=1) == {3, 4}
    assert expand_page_window({2}, total_pages=5, margin=2) == {0, 1, 2, 3, 4}
