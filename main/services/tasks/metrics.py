"""任务结果指标观测（spec 8.3）：generation_tasks_total(result) + 耗时直方图。

executor（FAILED/零卡失败）与 confirm 用例（COMPLETED——确认闭环下发布时点=用户确认）
共用；独立成模块避免 service ↔ executor 循环导入（executor 已反向 import service）。
"""

from datetime import datetime

from infra.db.models import Task
from infra.metrics import GENERATION_TASKS_DURATION_SECONDS, GENERATION_TASKS_TOTAL


def duration_seconds(start: str | None, end: str | None) -> float | None:
    """UTC ISO 字符串（format_utc 格式）耗时秒数；解析失败/缺失 → None（不观测）。"""
    if not start or not end:
        return None
    try:
        seconds = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(seconds.total_seconds(), 0.0)


def observe_task_result(task: Task, result: str) -> None:
    """8.3 generation_tasks_total(result) + generation_tasks_duration_seconds（started_at→ended_at）。"""
    GENERATION_TASKS_TOTAL.labels(result=result).inc()
    seconds = duration_seconds(task.started_at, task.ended_at)
    if seconds is not None:
        GENERATION_TASKS_DURATION_SECONDS.observe(seconds)
