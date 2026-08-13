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
            shlogging.set_context(suite="quick", scenario="api_smoke", user_id="u-1")
            shlogging.event("INFO", "请求完成", method="GET", path="/decks", status=200,
                            request_id="req-1", duration_ms=12, error_code="")
            line = json.loads(path.read_text().strip())
            self.assertEqual(line["run_id"], "run-123")
            self.assertEqual(line["level"], "INFO")
            self.assertEqual(line["message"], "请求完成")
            self.assertEqual(line["suite"], "quick")
            self.assertEqual(line["scenario"], "api_smoke")
            self.assertEqual(line["user_id"], "u-1")
            self.assertEqual(line["request_id"], "req-1")
            self.assertEqual(line["status"], 200)
            self.assertNotIn("device_id", line)  # 账号化:身份字段只有 user_id
            self.assertIn("timestamp", line)

    def test_empty_user_id_field_omitted(self) -> None:
        """runner 在会话建立前置空 user_id;空值不落字段。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-1", path)
            shlogging.set_context(suite="auth", scenario="auth", user_id="")
            shlogging.event("INFO", "before-auth", method="GET", path="/decks")
            line = json.loads(path.read_text().strip())
            self.assertNotIn("user_id", line)
            self.assertNotIn("device_id", line)

    def test_sensitive_fields_masked(self) -> None:
        """敏感字段统一脱敏:Authorization -> Bearer ***;password/token/api_key -> ***。"""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.log"
            shlogging.init_logger("run-1", path)
            shlogging.event(
                "INFO", "x",
                authorization="Bearer tok-abc", password="pw-abc",
                access_token="tok-abc", api_key="sk-abc", method="GET",
            )
            line = json.loads(path.read_text().strip())
            self.assertEqual(line["authorization"], "Bearer ***")
            self.assertEqual(line["password"], "***")
            self.assertEqual(line["access_token"], "***")
            self.assertEqual(line["api_key"], "***")
            self.assertEqual(line["method"], "GET")  # 非敏感字段原样

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
