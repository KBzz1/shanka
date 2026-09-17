"""services.chapters.triage 纯逻辑单元测试（V25-D-38 确定性分诊）。"""

from app.config import Settings
from services.chapters.triage import is_single_chapter_by_size


def _settings(threshold: int) -> Settings:
    return Settings(_env_file=None, single_chapter_max_chars=threshold)  # type: ignore[call-arg]


def test_triage_small_material_is_single_chapter() -> None:
    """总字符 ≤ 阈值 → 单章（闪卡问答类笔记 ~9k 落此分支）。"""
    assert is_single_chapter_by_size(9_276, _settings(24_000))
    assert is_single_chapter_by_size(24_000, _settings(24_000))  # 边界含等号


def test_triage_large_material_needs_structure() -> None:
    """总字符 > 阈值 → 需要结构路径（PDF 走 AI 兜底）。"""
    assert not is_single_chapter_by_size(24_001, _settings(24_000))
    assert not is_single_chapter_by_size(500_000, _settings(24_000))
