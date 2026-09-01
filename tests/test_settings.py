"""Tests for the centralized application settings contract."""

from aitos.config.settings import get_settings


def test_log_level_is_available_and_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = get_settings()

    assert settings.log_level == "INFO"


def test_log_level_is_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.log_level == "DEBUG"
