"""FSRS-6 排程单一适配（structure-contract 5.1；C-01/C-02/C-06/C-07）。

领域/业务层不直接 import fsrs：本模块是唯一入口。
参数（5.1 + 已确认决策，按 py-fsrs 6.3.2 实际 API 表达，见 task-1-report）：
- parameters：FSRS-6 默认 21 权重。py-fsrs 6.3.2 未导出 FSRS6_DEFAULT_PARAMETERS
  常量，本模块常量与其 Scheduler 默认权重逐值一致；
- desired_retention=0.9；
- learning_steps=(10m, 1d)、relearning_steps=(10m,)——v6 以 timedelta 表达间隔；
- maximum_interval=36500、enable_fuzzing=False（C-02 确定性）。
"""

from datetime import datetime, timedelta

from fsrs import Card, Rating, ReviewLog, Scheduler

from app.errors import AppError, ErrorCode

# 学习间隔（v6 以 timedelta 表达）：10m、1d
_LEARNING_STEPS = [timedelta(minutes=10), timedelta(days=1)]
_RELEARNING_STEPS = [timedelta(minutes=10)]

# FSRS-6 默认权重（21 参数，与 py-fsrs 6.3.2 Scheduler 默认值一致）
FSRS6_DEFAULT_PARAMETERS = (
    0.212,
    1.2931,
    2.3065,
    8.2956,
    6.4133,
    0.8334,
    3.0194,
    0.001,
    1.8722,
    0.1666,
    0.796,
    1.4835,
    0.0614,
    0.2629,
    1.6483,
    0.6014,
    1.8729,
    0.5425,
    0.0912,
    0.0658,
    0.1542,
)


def create_scheduler() -> Scheduler:
    """5.1 单一配置工厂（C-01 学习步 / C-02 确定性 / C-07 服务端统一配置）。"""
    return Scheduler(
        parameters=FSRS6_DEFAULT_PARAMETERS,
        desired_retention=0.9,
        learning_steps=_LEARNING_STEPS,
        relearning_steps=_RELEARNING_STEPS,
        maximum_interval=36500,
        enable_fuzzing=False,
    )


def review_card(
    scheduler: Scheduler,
    card: Card,
    rating: Rating,
    review_datetime: datetime | None = None,
) -> tuple[Card, ReviewLog]:
    """py-fsrs review_card 封装：返回 (new_card, review_log)。

    review_datetime 为可选显式复习时刻（默认 now/UTC）——C-02 确定性断言经它
    固定输入；Task 2 review service 可传事务内时钟时间。
    """
    return scheduler.review_card(card, rating, review_datetime=review_datetime)


def rating_from_str(value: str) -> Rating:
    """AGAIN/HARD/GOOD/EASY 映射；非法输入抛 REVIEW_EVENT_INVALID（契约 5.2/C-06）。"""
    mapping = {
        "AGAIN": Rating.Again,
        "HARD": Rating.Hard,
        "GOOD": Rating.Good,
        "EASY": Rating.Easy,
    }
    rating = mapping.get(value)
    if rating is None:
        raise AppError(ErrorCode.REVIEW_EVENT_INVALID, f"非法评级: {value}")
    return rating
