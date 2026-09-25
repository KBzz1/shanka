"""V25-D-42 连胜火苗推导纯函数单测（services.stats._streak_and_flames）。

语义（PRD V25-D-42 / structure-contract 3.12）：
- 每 2 个连续计数日攒 1 个火苗，上限 5；
- 已结束空缺日自动消耗 1 个火苗跳过（不计天数、不断卡），火苗耗尽清零重开；
- 今天无事件 = "待打"：不计数、不截断、不耗火苗（早晨不清零）；
- 火苗须在空缺日之前挣得（先攒后用）；火苗不跨 run 结转；
- max_streak_days = 历史各 run（含火苗吸收口径）最长计数日。
"""

from datetime import date

from services.stats.service import _streak_and_flames


def test_flames_absorb_single_gap_and_continue() -> None:
    """6 天 3 火苗断 1 天后复卡：连胜续到 7，火苗 3−1=2（用户核心场景）。"""
    days = {f"2026-08-{d:02d}" for d in range(2, 8)}  # 8/02–8/07 连续 6 天
    days.add("2026-08-09")  # 8/08 空缺（火苗吸收），8/09 复卡
    assert _streak_and_flames(days, date(2026, 8, 9)) == (7, 2, 1, 7)


def test_today_pending_keeps_streak() -> None:
    """今天尚未复习（早晨）：连胜与火苗保持，不清零不消耗。"""
    days = {f"2026-08-{d:02d}" for d in range(5, 11)}  # 8/05–8/10 连续 6 天
    assert _streak_and_flames(days, date(2026, 8, 11)) == (6, 3, 0, 6)


def test_gap_before_first_flame_breaks() -> None:
    """1 天连胜（0 火苗）遇空缺即断：旧 run 冻结为 max，新 run 从下一事件重开。"""
    days = {"2026-08-05", "2026-08-07"}  # 8/06 空缺且无火苗可挡
    assert _streak_and_flames(days, date(2026, 8, 7)) == (1, 0, 0, 1)


def test_consecutive_gaps_each_consume_one_flame_then_reset() -> None:
    """6 天 3 火苗连断 4 天：挡 3 天后第 4 天截断，清零重开。"""
    days = {f"2026-08-{d:02d}" for d in range(1, 7)}  # 8/01–8/06 连续 6 天
    days.add("2026-08-11")  # 8/07–8/10 连断 4 天后复卡
    assert _streak_and_flames(days, date(2026, 8, 11)) == (1, 0, 0, 6)


def test_flames_capped_at_five() -> None:
    """14 天连胜火苗封顶 5。"""
    days = {f"2026-07-{d:02d}" for d in range(29, 32)} | {
        f"2026-08-{d:02d}" for d in range(1, 12)
    }  # 7/29–8/11 连续 14 天
    assert _streak_and_flames(days, date(2026, 8, 11)) == (14, 5, 0, 14)


def test_flames_not_carried_across_runs() -> None:
    """火苗不跨 run 结转：旧 run 6 天截断后，新 run 从 0 重新攒。"""
    days = {f"2026-07-{d:02d}" for d in range(1, 7)}  # 7/01–7/06（3 火苗）
    days |= {f"2026-08-{d:02d}" for d in range(9, 12)}  # 断很久后 8/09–8/11 新 run
    assert _streak_and_flames(days, date(2026, 8, 11)) == (3, 1, 0, 6)


def test_flame_must_be_earned_before_gap() -> None:
    """先攒后用：空缺日出现时 run 内未挣得的火苗不可由之后的计数日追补。"""
    days = {"2026-08-05", "2026-08-07", "2026-08-08"}  # 8/06 空缺时 run 仅 1 天（0 火苗）
    assert _streak_and_flames(days, date(2026, 8, 8)) == (2, 1, 0, 2)


def test_empty_days_all_zero() -> None:
    assert _streak_and_flames(set(), date(2026, 8, 11)) == (0, 0, 0, 0)
