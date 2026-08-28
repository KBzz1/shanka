"""app.config 单一配置入口单元测试。"""

from pathlib import Path

import pytest

from app.config import Settings


def test_settings_defaults() -> None:
    # _env_file=None：纯默认值断言，不受仓库根 .env 加载影响（显式传参构造约定）
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_name == "shanka-backend"
    assert settings.version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_url == "sqlite:///./shanka.db"
    assert settings.storage_path == Path("./storage")
    assert settings.rate_limit_write_per_minute == 60
    assert settings.rate_limit_ip_per_second == 5
    assert settings.rate_limit_api_key_per_hour == 10
    assert settings.rate_limit_samples_per_hour == 20
    assert settings.rate_limit_pdf_per_hour == 10
    assert settings.pdf_max_size_bytes == 100 * 1024 * 1024
    assert settings.pdf_max_pages == 1000
    assert settings.deepseek_api_key is None
    assert settings.api_key_encryption_key is None
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_thinking is False
    assert settings.deepseek_timeout_seconds == 60.0
    assert settings.generation_work_quantum_batches == 4


def test_new_hard_limits_defaults() -> None:
    # LLM 链路升级（spec §10 硬上限、§8 scoring、§6.2/§6.3 planning）9 个硬上限/预算字段默认值
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.planner_max_input_chars == 20_000
    assert settings.max_generation_units_per_task == 300
    assert settings.max_planner_groups_per_task == 30
    assert settings.max_source_pages_per_unit == 8
    assert settings.generator_max_input_chars == 10_000
    assert settings.max_scoring_calls_per_task == 60
    assert settings.scoring_max_cards_per_call == 12
    assert settings.scoring_max_input_chars == 15_000
    assert settings.planning_retry_limit == 2
    # §5.7/§10 输出上限（JSON 截断防线；Scoring 每次仍按 item 数取更小实际值）
    assert settings.planner_max_output_tokens == 2048
    assert settings.generator_max_output_tokens == 768
    assert settings.rewrite_max_output_tokens == 768
    assert settings.scoring_max_output_tokens == 4096


def test_settings_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert Settings(_env_file=None).database_url == "sqlite:///:memory:"  # type: ignore[call-arg]


def test_settings_explicit_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert (
        Settings(database_url="sqlite:///./explicit.db").database_url == "sqlite:///./explicit.db"
    )


def test_settings_secret_hidden_in_repr() -> None:
    settings = Settings(deepseek_api_key="sk-super-secret-value")
    assert "sk-super-secret-value" not in repr(settings)


def test_settings_encryption_key_hidden_in_repr() -> None:
    settings = Settings(api_key_encryption_key="aa" * 32)
    assert "aa" * 32 not in repr(settings)
