"""shanka.logging 的单元测试(stdlib unittest,零依赖)。"""
import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import logging as shlogging


class LoggingTest(unittest.TestCase):
    def test_json_line_fields(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-123", path)
            shlogging.set_context(suite="quick", scenario="api_smoke", device_id="dev-1")
            shlogging.event("INFO", "请求完成", method="GET", path="/decks", status=200,
                            request_id="req-1", duration_ms=12, error_code="")
            line = json.loads(path.read_text().strip())
            self.assertEqual(line["run_id"], "run-123")
            self.assertEqual(line["level"], "INFO")
            self.assertEqual(line["message"], "请求完成")
            self.assertEqual(line["suite"], "quick")
            self.assertEqual(line["scenario"], "api_smoke")
            self.assertEqual(line["device_id"], "dev-1")
            self.assertEqual(line["request_id"], "req-1")
            self.assertEqual(line["status"], 200)
            self.assertIn("timestamp", line)

    def test_append_mode(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-1", path)
            shlogging.event("INFO", "a")
            shlogging.init_logger("run-2", path)  # 第二次初始化复用同一文件
            shlogging.event("INFO", "b")
            lines = path.read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["run_id"], "run-2")


if __name__ == "__main__":
    unittest.main()
