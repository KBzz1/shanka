"""FSRS-6 排程单一适配（structure-contract 5.1；C-01/C-02/C-06/C-07）。

领域/业务层不直接 import fsrs：本模块是唯一入口。唯一入口扩展（I-1，final review）：
fsrs 类型构造能力亦由本模块导出——build_fsrs_card（ReviewState 快照 → fsrs Card）、
state_upper（fsrs State 名 → 契约 3.10 大写枚举），review service 经此交互，不再直接
import fsrs。
参数（5.1 + 已确认决策，按 py-fsrs 4.1.2 实际 API 表达；V2 fix round 1 固定 4.x 线，
R-13 裁决 3 步学习步，详见 task-1-report）：
- parameters：FSRS-6 默认权重（4.1.2 Scheduler 默认 19 参数；py-fsrs 未导出
  FSRS6_DEFAULT_PARAMETERS 常量，本模块常量与其默认值逐值一致）；
- desired_retention=0.9；
- learning_steps=(10m, 10m, 1d)、relearning_steps=(10m,)——3 步为 R-13 裁决：
  py-fsrs 语义下 GOOD 间隔 = steps[step+1]，3 步配置使新卡首 GOOD → 10m、
  二次 → 1d、三次 → 毕业 Review，复现 5.2 表并符合 C-01 决策意图
  （新卡 10 分钟后复现,次日复现后毕业）；v4 以 timedelta 表达间隔；
- maximum_interval=36500、enable_fuzzing=False（C-02 确定性）。
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fsrs import Card, Rating, Scheduler, State

from app.errors import AppError, ErrorCode

# 适配器公开面（mypy strict no-implicit-reexport：Card 类型经此显式导出，review service 复用）
__all__ = [
    "Card",
    "build_fsrs_card",
    "create_scheduler",
    "rating_from_str",
    "review_card",
    "state_upper",
]

# 学习间隔（R-13 裁决 3 步；v4 以 timedelta 表达）：10m、10m、1d
_LEARNING_STEPS = [timedelta(minutes=10), timedelta(minutes=10), timedelta(days=1)]
_RELEARNING_STEPS = [timedelta(minutes=10)]

# 快照 due/last_review 字符串格式（database-design §0：format_utc 输出，恒 3 位毫秒 Z）
_DUE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# fsrs State 名（.name，如 "Learning"）→ 契约 3.10 大写枚举（落库口径，裁决 1）
_STATE_UPPER = {
    "Learning": "LEARNING",
    "Review": "REVIEW",
    "Relearning": "RELEARNING",
}

# FSRS-6 默认权重（19 参数，与 py-fsrs 4.1.2 Scheduler 默认值一致）
FSRS6_DEFAULT_PARAMETERS = (
    0.40255,
    1.18385,
    3.173,
    15.69105,
    7.1949,
    0.5345,
    1.4604,
    0.0046,
    1.54575,
    0.1192,
    1.01925,
    1.9395,
    0.11,
    0.29605,
    2.2698,
    0.2315,
    2.9898,
    0.51655,
    0.6621,
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
) -> tuple[Card, Any]:
    """py-fsrs review_card 封装：返回 (new_card, review_log)。

    review_datetime 为可选显式复习时刻（默认 now/UTC）——C-02 确定性断言经它固定输入；
    Task 2 review service 可传事务内时钟时间。fsrs 4.x 无类型桩（mypy override 视为
    Any），返回类型按 brief 契约声明为 tuple[Card, Any]。
    """
    return cast(
        tuple[Card, Any], scheduler.review_card(card, rating, review_datetime=review_datetime)
    )


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


def _parse_utc(value: str) -> datetime:
    """database-design §0 时间戳字符串（恒 3 位毫秒 Z）→ aware UTC datetime。"""
    return datetime.strptime(value, _DUE_FORMAT).replace(tzinfo=UTC)


def build_fsrs_card(
    *,
    stability: float | None,
    difficulty: float | None,
    due: str,
    last_review: str | None,
    reps: int,
    lapses: int,
    state: str,
    learning_step: int,
) -> Card:
    """ReviewState 快照 → py-fsrs Card（I-1：fsrs 类型构造能力唯一入口扩展）。

    state 大写 → fsrs State 映射（契约 3.10 枚举值域）：
    - NEW / LEARNING → Learning，step=learning_step（NEW 时调用方传 0，与 fsrs
      对 Learning 的默认 step=0 口径一致）；
    - REVIEW → Review（step=None）；
    - RELEARNING → Relearning，step 恒 0——relearning_steps 单步（10m），且 fsrs 仅对
      Learning 默认 step=0，Relearning step=None 会在 review_card 断言失败。

    stability/difficulty 首评路径：NEW（快照占位值 0.0/1.0 不可直接输入——stability=0.0
    + last_review=None → retrievability=0 → _next_stability log(0) ValueError，裁决 3）
    或双 None 时走 fsrs 默认初始化（不传 stability/difficulty/last_review，review_card
    首评分支）。

    reps/lapses：py-fsrs 4.x Card 无此属性（自计数在 review service）——入参仅保持
    快照→Card 适配签名完整，不参与构造。
    """
    if state == "REVIEW":
        fsrs_state = State.Review
        step = None
    elif state == "RELEARNING":
        fsrs_state = State.Relearning
        step = 0
    else:  # NEW / LEARNING
        fsrs_state = State.Learning
        step = learning_step
    if state == "NEW" or (stability is None and difficulty is None):
        return Card(state=fsrs_state, step=step, due=_parse_utc(due))
    return Card(
        stability=stability,
        difficulty=difficulty,
        state=fsrs_state,
        step=step,
        due=_parse_utc(due),
        last_review=_parse_utc(last_review) if last_review else None,
    )


def state_upper(state_name: str) -> str:
    """fsrs State 名称（Learning/Review/Relearning）→ 契约 3.10 大写枚举（落库口径，裁决 1）。"""
    try:
        return _STATE_UPPER[state_name]
    except KeyError as exc:
        raise ValueError(f"未知 fsrs state: {state_name}") from exc
