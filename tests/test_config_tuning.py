from __future__ import annotations

import pytest
from pydantic import ValidationError

from aitos.config.tuning import TuningSettings, load_tuning_settings


def test_balanced_profile_defaults_are_loaded():
    settings = load_tuning_settings(profile="balanced", mode="paper")
    assert settings.profile == "balanced"
    assert settings.mode == "paper"
    assert settings.scanner.top_n == 5
    assert settings.analysis.expensive_analysis_concurrency == 2
    assert settings.positions.max_open_positions == 3


def test_low_resource_profile_reduces_workload():
    low = load_tuning_settings(profile="low_resource", mode="paper")
    balanced = load_tuning_settings(profile="balanced", mode="paper")
    assert low.scanner.universe_limit < balanced.scanner.universe_limit
    assert low.analysis.expensive_analysis_concurrency == 1
    assert low.positions.max_positions_per_symbol == 1


def test_allowlist_mode_requires_symbols():
    with pytest.raises(ValidationError):
        TuningSettings.model_validate(
            {
                "profile": "balanced",
                "mode": "paper",
                "scanner": {
                    "universe_mode": "allowlist",
                    "symbol_allowlist": [],
                },
            }
        )


def test_invalid_numeric_ranges_are_rejected():
    with pytest.raises(ValidationError):
        TuningSettings.model_validate({"scanner": {"top_n": 0}})


def test_environment_override_wins_over_profile(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_SCANNER_TOP_N", "7")
    settings = load_tuning_settings(profile="balanced", mode="paper")
    assert settings.scanner.top_n == 7


def test_invalid_environment_override_fails_clearly(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_SCANNER_TOP_N", "many")
    with pytest.raises(ValueError, match="AITOS_TUNING_SCANNER_TOP_N"):
        load_tuning_settings(profile="balanced", mode="paper")


def test_settings_exposes_resolved_tuning(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_PROFILE", "low_resource")
    from aitos.config.settings import AITOSSettings

    assert AITOSSettings().tuning.profile == "low_resource"


def test_live_mode_does_not_enable_adaptive_tuning_by_default():
    settings = load_tuning_settings(profile="high_performance", mode="live")
    assert settings.mode == "live"
    assert settings.runtime.adaptive_tuning_enabled is False
    assert settings.runtime.config_reload_enabled is False


def test_event_bus_profile_exposes_pool_and_batch_budgets():
    low = load_tuning_settings(profile="low_resource", mode="paper")
    balanced = load_tuning_settings(profile="balanced", mode="paper")
    high = load_tuning_settings(profile="high_performance", mode="paper")
    assert low.event_bus.consumer_concurrency < balanced.event_bus.consumer_concurrency
    assert low.event_bus.connection_pool_size < balanced.event_bus.connection_pool_size
    assert balanced.event_bus.consumer_batch_size < high.event_bus.consumer_batch_size


def test_live_position_limit_is_part_of_selected_profile():
    settings = load_tuning_settings(profile="low_resource", mode="live")
    assert settings.positions.max_open_positions == 2
    assert settings.runtime.adaptive_tuning_enabled is False



def test_effective_tuning_report_is_secret_free_and_stable():
    from aitos.config.tuning import effective_tuning_report

    settings = load_tuning_settings(profile="balanced", mode="paper")
    first = effective_tuning_report(settings)
    second = effective_tuning_report(settings)
    assert first["config_version"] == second["config_version"]
    assert first["profile"] == "balanced"
    assert first["mode"] == "paper"
    assert "api_key" not in str(first).lower()


def test_runtime_reload_environment_override_is_validated(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED", "true")
    assert load_tuning_settings(profile="balanced", mode="paper").runtime.config_reload_enabled
    monkeypatch.setenv("AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED"):
        load_tuning_settings(profile="balanced", mode="paper")



def test_intensive_exit_analysis_budget_is_bounded_per_window():
    import time

    from aitos.trading.lifecycle import TradeLifecycle

    base = load_tuning_settings(profile="balanced", mode="paper")
    positions = base.positions.model_copy(
        update={"monitoring_interval_seconds": 60, "intensive_analysis_budget": 1}
    )
    lifecycle = object.__new__(TradeLifecycle)
    lifecycle._tuning = base.model_copy(update={"positions": positions})
    lifecycle._intensive_window_started_at = time.monotonic()
    lifecycle._intensive_analyses_used = 0

    assert lifecycle._allow_intensive_analysis() is True
    assert lifecycle._allow_intensive_analysis() is False

    lifecycle._intensive_window_started_at -= 61
    assert lifecycle._allow_intensive_analysis() is True
