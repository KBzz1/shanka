"""app.config 单一配置入口单元测试。"""

from pathlib import Path

import pytest

from app.config import Settings


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.app_name == "shanka-backend"
    assert settings.version == "0.1.0"
    assert settings.environment == "development"
    assert settings.database_url == "sqlite:///./shanka.db"
    assert settings.storage_path == Path("./storage")
    assert settings.deepseek_api_key is None


def test_settings_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert Settings().database_url == "sqlite:///:memory:"


def test_settings_explicit_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    assert (
        Settings(database_url="sqlite:///./explicit.db").database_url == "sqlite:///./explicit.db"
    )


def test_settings_secret_hidden_in_repr() -> None:
    settings = Settings(deepseek_api_key="sk-super-secret-value")
    assert "sk-super-secret-value" not in repr(settings)
