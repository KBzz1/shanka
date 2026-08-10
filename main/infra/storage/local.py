"""本地文件存储（infra/storage）。

F0：check_writable 就绪探测（structure-contract 8.2）；V3A 扩展受控 PDF 存储：
- save(data) -> storage_key：随机 UUID hex 为文件名（1.7：禁止含用户输入），
  按 storage_key 前缀 4 位分目录（storage_key[:2]/[2:4]，避免单目录膨胀）；
- open(storage_key) -> Path：严格校验 32 位 hex（路径穿越防护）；
- delete(storage_key)：删除文件（不存在静默）。
"""

import os
import re
import uuid
from pathlib import Path

_UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


class LocalStorage:
    def __init__(self, storage_path: Path) -> None:
        self.storage_path = storage_path

    def check_writable(self) -> bool:
        """就绪探针（structure-contract 8.2）：目录可创建且可写。失败返回 False，不抛异常。"""
        try:
            self.storage_path.mkdir(parents=True, exist_ok=True)
            probe = self.storage_path / f".write-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False

    def save(self, data: bytes) -> str:
        """保存文件，返回 storage_key（随机 UUID hex）。"""
        storage_key = uuid.uuid4().hex
        target = self._path_for(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return storage_key

    def open(self, storage_key: str) -> Path:
        """返回存储对象路径（不存在不报错——由调用方处理）。"""
        if not _UUID_HEX_RE.fullmatch(storage_key):
            raise ValueError("非法 storage_key")
        return self._path_for(storage_key)

    def delete(self, storage_key: str) -> None:
        target = self.open(storage_key)
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    def _path_for(self, storage_key: str) -> Path:
        return self.storage_path / storage_key[:2] / storage_key[2:4] / storage_key
