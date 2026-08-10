"""契约守卫 4：localization_key ↔ 文案清单（project-structure 5；R-01 派生规则与唯一位置）。"""

import re

from app.errors import LOCALIZATION_KEYS, ErrorCode, localization_key


def test_localization_keys_match_derived_set() -> None:
    derived = frozenset(localization_key(code) for code in ErrorCode)
    assert derived == LOCALIZATION_KEYS


def test_localization_keys_format() -> None:
    for key in LOCALIZATION_KEYS:
        assert re.fullmatch(r"error\.[a-z0-9_]+", key) is not None
