"""shanka.environments 单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shanka.environments import ENVIRONMENTS, is_prod, resolve


class EnvironmentsTest(unittest.TestCase):
    def test_local_and_prod(self) -> None:
        self.assertEqual(resolve("local"), "http://localhost:8000")
        self.assertEqual(resolve("prod"), "https://shanka.kbzz1.top")

    def test_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve("nonexistent")

    def test_is_prod(self) -> None:
        self.assertFalse(is_prod("local"))
        self.assertTrue(is_prod("prod"))


if __name__ == "__main__":
    unittest.main()
