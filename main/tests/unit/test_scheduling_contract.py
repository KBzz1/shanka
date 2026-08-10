"""services.scheduling 排程契约确定性断言（structure-contract 5.1 表，fuzzing 关闭）。

校准说明（py-fsrs 4.1.2，V2 fix round 1 固定 4.x 线 + R-13 裁决 3 步学习步，见
task-1-report）：
- learning_steps=(10m, 10m, 1d)：py-fsrs 语义下 GOOD 间隔 = steps[step+1]，3 步配置
  使新卡首 GOOD → 10m、二次 → 1d、三次 → 毕业 Review，与 5.2 表逐行一致；
- relearning_steps=(10m,)；v4 以 timedelta 表达间隔；
- Card() 即"新卡"：State.Learning、step=0、due=now（4.x/6.x 无 State.New，仅 3.x 有）；
- State 为 IntEnum，断言统一用 .name（str() 输出数字）。
"""

from datetime import UTC, datetime, timedelta

import pytest
from fsrs import Card, Rating

from app.errors import AppError, ErrorCode
from services.scheduling.scheduler import create_scheduler, rating_from_str, review_card


def test_scheduling_new_card_good_learning_plus_10m() -> None:
    """新卡首次 GOOD → LEARNING，due ≈ now + 10m（5.1 表）。"""
    scheduler = create_scheduler()
    new_card, _ = review_card(scheduler, Card(), Rating.Good)
    assert new_card.state.name == "Learning"
    expected = datetime.now(UTC) + timedelta(minutes=10)
    assert abs((new_card.due - expected).total_seconds()) < 60


def test_scheduling_second_good_learning_plus_1d() -> None:
    """新卡第二次 GOOD → LEARNING，due ≈ now + 1d（5.1 表）。"""
    scheduler = create_scheduler()
    card, _ = review_card(scheduler, Card(), Rating.Good)
    new_card, _ = review_card(scheduler, card, Rating.Good)
    assert new_card.state.name == "Learning"
    expected = datetime.now(UTC) + timedelta(days=1)
    assert abs((new_card.due - expected).total_seconds()) < 120


def test_scheduling_third_good_review() -> None:
    """第三次 GOOD → REVIEW，due 按 FSRS 计算（> 1d）。"""
    scheduler = create_scheduler()
    card, _ = review_card(scheduler, Card(), Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Good)
    new_card, _ = review_card(scheduler, card, Rating.Good)
    assert new_card.state.name == "Review"
    assert new_card.due > datetime.now(UTC) + timedelta(days=1)


def test_scheduling_review_again_relearning_plus_10m() -> None:
    """REVIEW 中 AGAIN → RELEARNING，due ≈ now + 10m（5.1 表）。"""
    scheduler = create_scheduler()
    card, _ = review_card(scheduler, Card(), Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Good)
    new_card, _ = review_card(scheduler, card, Rating.Again)
    assert new_card.state.name == "Relearning"
    expected = datetime.now(UTC) + timedelta(minutes=10)
    assert abs((new_card.due - expected).total_seconds()) < 60


def test_scheduling_relearning_good_review() -> None:
    """RELEARNING 中 GOOD → REVIEW（5.1 表）。"""
    scheduler = create_scheduler()
    card, _ = review_card(scheduler, Card(), Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Good)
    card, _ = review_card(scheduler, card, Rating.Again)
    new_card, _ = review_card(scheduler, card, Rating.Good)
    assert new_card.state.name == "Review"


def test_scheduling_same_input_same_output_deterministic() -> None:
    """C-02 fuzzing 关闭：同输入（含 review_datetime）同输出。"""
    scheduler = create_scheduler()
    review_dt = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    c1, _ = review_card(scheduler, Card(), Rating.Hard, review_datetime=review_dt)
    c2, _ = review_card(scheduler, Card(), Rating.Hard, review_datetime=review_dt)
    assert c1.due == c2.due
    assert c1.stability == c2.stability


def test_scheduling_rating_from_str() -> None:
    assert rating_from_str("AGAIN") is Rating.Again
    assert rating_from_str("HARD") is Rating.Hard
    assert rating_from_str("GOOD") is Rating.Good
    assert rating_from_str("EASY") is Rating.Easy


def test_scheduling_rating_invalid_raises() -> None:
    with pytest.raises(AppError) as excinfo:
        rating_from_str("MAYBE")
    assert excinfo.value.code is ErrorCode.REVIEW_EVENT_INVALID
