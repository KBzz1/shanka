"""本地文件存储（infra/storage）：F0 提供就绪探针可写性检查；PDF 受控存储在 V3A 扩展。"""

import os
import uuid
from pathlib import Path


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
