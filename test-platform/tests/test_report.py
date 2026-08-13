"""shanka.report 单元测试:PASS/FAIL 计数、报告字段与 summary 复位(多场景不串)。"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka import report


class ReportTest(unittest.TestCase):
    def tearDown(self) -> None:
        report.STEPS.clear()
        report.META.clear()

    def test_summary_counts_and_resets(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.check("a", True)
            report.check("b", False)
            failed = report.summary()
            report.check("c", True)
            failed2 = report.summary()
        self.assertEqual(failed, 1)
        self.assertEqual(failed2, 0)  # summary 后复位,第二次只统计 c
        out = buf.getvalue()
        self.assertIn("1/2 通过, 1 失败", out)
        self.assertIn("1/1 通过, 0 失败", out)

    def test_record_field_printed(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.record("local_test_users_created", 2)
            report.check("a", True)
            report.summary()
        self.assertIn("报告字段: local_test_users_created=2", buf.getvalue())

    def test_record_reset_after_summary(self) -> None:
        with redirect_stdout(io.StringIO()):
            report.record("local_test_users_created", 2)
            report.summary()
        buf = io.StringIO()
        with redirect_stdout(buf):
            report.check("x", True)
            report.summary()
        self.assertNotIn("报告字段", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
