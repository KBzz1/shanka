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
from typing import Any

import pytest
from fsrs import Card, Rating

from app.errors import AppError, ErrorCode
from services.scheduling.scheduler import (
    build_fsrs_card,
    create_scheduler,
    rating_from_str,
    review_card,
    state_upper,
)


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


def _snapshot(**overrides: Any) -> dict[str, Any]:
    """ReviewState 快照风格入参（build_fsrs_card 关键字参数）。"""
    kwargs: dict[str, Any] = {
        "stability": 5.0,
        "difficulty": 1.0,
        "due": "2026-08-11T00:00:00.000Z",
        "last_review": "2026-08-11T00:00:00.000Z",
        "reps": 3,
        "lapses": 0,
        "state": "REVIEW",
        "learning_step": 0,
    }
    kwargs.update(overrides)
    return kwargs


def test_scheduling_build_fsrs_card_new_first_review_init() -> None:
    """I-1：NEW → Learning；快照占位 stability 0.0/difficulty 1.0 不传入（fsrs 首评初始化，裁决 3）。"""
    card = build_fsrs_card(**_snapshot(stability=0.0, difficulty=1.0, state="NEW"))
    assert card.state.name == "Learning"
    assert card.stability is None
    assert card.difficulty is None
    assert card.last_review is None
    assert card.step == 0


def test_scheduling_build_fsrs_card_state_mapping_and_steps() -> None:
    """I-1：大写状态映射 + step 语义——LEARNING 用 learning_step；REVIEW step=None；RELEARNING 恒 0。"""
    learning = build_fsrs_card(**_snapshot(state="LEARNING", learning_step=1))
    assert learning.state.name == "Learning"
    assert learning.step == 1
    assert learning.stability == 5.0  # 非 NEW：快照值直传
    review = build_fsrs_card(**_snapshot(state="REVIEW"))
    assert review.state.name == "Review"
    assert review.step is None
    relearning = build_fsrs_card(**_snapshot(state="RELEARNING"))
    assert relearning.state.name == "Relearning"
    assert relearning.step == 0  # relearning_steps 单步；fsrs 仅对 Learning 默认 step=0


def test_scheduling_state_upper_mapping() -> None:
    """I-1：fsrs State 名 → 契约 3.10 大写枚举（落库口径，裁决 1）。"""
    assert state_upper("Learning") == "LEARNING"
    assert state_upper("Review") == "REVIEW"
    assert state_upper("Relearning") == "RELEARNING"
    with pytest.raises(ValueError):
        state_upper("New")  # 4.x 无 State.New——未知名显式报错
