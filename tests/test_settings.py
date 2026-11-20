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



def test_binance_credentials_are_selected_by_environment(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "mainnet-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "mainnet-secret")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_SECRET", "testnet-secret")
    monkeypatch.setenv("BINANCE_TESTNET", "true")

    settings = get_settings()

    assert settings.binance.execution_credentials == ("testnet-key", "testnet-secret")


def test_mainnet_requires_mainnet_credentials_even_when_testnet_keys_exist(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "mainnet-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "mainnet-secret")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_SECRET", "testnet-secret")
    monkeypatch.setenv("BINANCE_TESTNET", "false")

    settings = get_settings()

    assert settings.binance.execution_credentials == ("mainnet-key", "mainnet-secret")


def test_explicit_futures_environment_overrides_legacy_testnet_setting(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("AITOS_BINANCE_FUTURES_ENVIRONMENT", "mainnet")
    monkeypatch.setenv("BINANCE_API_KEY", "mainnet-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "mainnet-secret")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_FUTURES_TESTNET_API_SECRET", "testnet-secret")

    settings = get_settings()

    assert settings.binance.testnet is False
    assert settings.binance.execution_credentials == ("mainnet-key", "mainnet-secret")
