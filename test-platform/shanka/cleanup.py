"""数据策略:场景创建的资源登记与结束清理(账号域:资源按 user 隔离,结束清理不留残留)。"""

from __future__ import annotations

from shanka import logging as shlogging
from shanka.client import ShankaClient


class DataScope:
    def __init__(self, client: ShankaClient) -> None:
        self._client = client
        self._decks: list[str] = []

    def create_deck(self, name: str, project_id: str | None = None) -> str:
        """创建牌组并登记清理;project_id 归属学习项目(V2.5,牌组须同项目才能挂任务)。"""
        body: dict = {"name": name}
        if project_id is not None:
            body["project_id"] = project_id
        r = self._client.request("POST", "/decks", body=body, idempotent=True, step="deck-create")
        if r.status not in (200, 201):
            raise RuntimeError(f"创建牌组失败: {r.status} {r.json}")
        deck_id = str(r.json["deck_id"])
        self._decks.append(deck_id)
        return deck_id

    def cleanup(self) -> None:
        for deck_id in self._decks:
            r = self._client.request("DELETE", f"/decks/{deck_id}", idempotent=True, step="deck-cleanup")
            if r.status not in (200, 204):
                shlogging.event("WARN", "清理牌组失败", deck_id=deck_id, status=r.status)
        self._decks.clear()
