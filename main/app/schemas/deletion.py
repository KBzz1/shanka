"""Deletion preflight response models (project/deck resource coordination)."""

from typing import Any

from pydantic import BaseModel


class DeletionTaskBlocker(BaseModel):
    task_id: str
    status: str
    internal_stage: str | None = None
    project_id: str | None = None
    deck_id: str | None = None
    can_abandon: bool
    allowed_actions: list[str]


class DeletionPreflight(BaseModel):
    resource_type: str
    resource_id: str
    can_delete: bool
    blockers: list[DeletionTaskBlocker]
    abandonable_task_ids: list[str]
    has_uncancellable_tasks: bool
    actions: list[str]
    impact: dict[str, Any]
