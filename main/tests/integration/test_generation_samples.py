"""样卡生成集成测试（V2.5 Task 5）：构成/启用难度/指纹/校验（真实 SQLite 语义的纯函数）。

V2.5 起样卡持久化于任务（POST /tasks/{task_id}/samples → worker 完成），旧 /samples
兼容路径移除；本文件覆盖样卡生成的纯函数（services/generation/samples.py）：
- 1~3 张样卡与启用难度一一对应（比例为 0 的难度不生成，契约 3.5）；
- 每张样卡为轻量组件（3.13）；不入库（任务持久化由 test_v25_sample_persistence 覆盖）；
- sample_config_hash 配置指纹确定性；
- 配置校验（validate_config，INVALID_PREFERENCES 语义）。
"""

import uuid

import pytest

from app.errors import AppError, ErrorCode
from app.schemas.samples import DifficultyRatio, GenerationConfig
from services.generation.samples import config_fingerprint, sample_cards
from services.generation.validate import validate_config


def _uuid() -> str:
    return str(uuid.uuid4())


def _config(coverage_mode: str = "BALANCED") -> GenerationConfig:
    return GenerationConfig(
        coverage_mode=coverage_mode,
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )


def test_samples_three_cards_all_difficulties() -> None:
    """三档全启用 → 3 张（1 基础 + 1 理解 + 1 深问；V2.5 难度改名）；DEEP_QUESTION 只允许
    QUESTION 卡型（契约 3.6）。"""
    cards = sample_cards(_config(), chapter_name="第一章", task_id=_uuid())
    assert len(cards) == 3
    assert {c["target_difficulty"] for c in cards} == {
        "BASIC",
        "UNDERSTANDING",
        "DEEP_QUESTION",
    }
    assert sum(1 for c in cards if c["card_type"] == "QUESTION") == 3
    assert sum(1 for c in cards if c["card_type"] == "TRUE_FALSE") == 0
    # R-14：SampleCard 轻量组件（structure-contract 3.13）——无落库/归属/版本占位字段
    for card in cards:
        assert {"card_id", "front", "back", "card_type"} <= set(card)
        assert {"deck_id", "position", "created_at", "updated_at"} & set(card) == set()


def test_samples_only_enabled_difficulties() -> None:
    """比例为 0 的难度不生成（契约 3.5）：禁用理解档 → 2 张；仅基础档 → 1 张。"""
    cards = sample_cards(
        GenerationConfig(
            coverage_mode="BALANCED",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=0, deep_question=60),
        ),
        chapter_name="第一章",
        task_id=_uuid(),
    )
    assert len(cards) == 2
    assert {c["target_difficulty"] for c in cards} == {"BASIC", "DEEP_QUESTION"}
    only_basic = sample_cards(
        GenerationConfig(
            coverage_mode="BALANCED",
            difficulty_ratio=DifficultyRatio(basic=100, understanding=0, deep_question=0),
        ),
        chapter_name="第一章",
        task_id=_uuid(),
    )
    assert len(only_basic) == 1
    assert only_basic[0]["target_difficulty"] == "BASIC"


def test_samples_fingerprint_deterministic_and_sensitive() -> None:
    """配置指纹：同配置同值、不同配置不同值；dict/模型两路径同值（start 校验口径）。"""
    assert config_fingerprint(_config()) == config_fingerprint(_config())
    assert config_fingerprint(_config()) == config_fingerprint(_config().model_dump())
    different = GenerationConfig(
        coverage_mode="EXTENSIVE",
        difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
    )
    assert config_fingerprint(different) != config_fingerprint(_config())


def test_samples_validate_config() -> None:
    validate_config(_config())  # 合法
    # V2.5：非法比例/非法 coverage_mode 由 Pydantic 模型层拦截（构造即 ValidationError）
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        DifficultyRatio(basic=50, understanding=50, deep_question=20)  # 合计 120 非法
    with pytest.raises(pydantic.ValidationError):
        GenerationConfig(
            coverage_mode="HUGE",
            difficulty_ratio=DifficultyRatio(basic=40, understanding=40, deep_question=20),
        )
    # service 层兜底（model_construct 绕过模型 validator 的防御路径）：
    # 比例语义非法 / coverage_mode 值域非法 → INVALID_PREFERENCES（V2.5 语义）
    bypassed_ratio = GenerationConfig.model_construct(
        coverage_mode="BALANCED",
        difficulty_ratio=DifficultyRatio.model_construct(
            basic=50, understanding=50, deep_question=20
        ),
    )
    with pytest.raises(AppError) as excinfo:
        validate_config(bypassed_ratio)
    assert excinfo.value.code is ErrorCode.INVALID_PREFERENCES
    bypassed_mode = GenerationConfig.model_construct(
        coverage_mode="HUGE",
        difficulty_ratio=DifficultyRatio.model_construct(
            basic=40, understanding=40, deep_question=20
        ),
    )
    with pytest.raises(AppError) as excinfo:
        validate_config(bypassed_mode)
    assert excinfo.value.code is ErrorCode.INVALID_PREFERENCES
