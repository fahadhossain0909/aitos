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


def test_dead_candle_and_liquidity_guard_defaults_and_env_overrides(monkeypatch):
    defaults = load_tuning_settings(profile="balanced", mode="paper")
    assert defaults.scanner.min_liquidity_quality == 5.0
    assert defaults.scanner.max_directional_cancel_pressure == 0.6
    assert defaults.positions.dead_candle_exit_bars == 16
    assert defaults.positions.dead_candle_max_progress_r == 0.10
    assert defaults.positions.dead_candle_entry_band_pct == 0.002
    assert defaults.positions.stale_position_max_hours == 6.0

    monkeypatch.setenv("AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY", "5.5")
    monkeypatch.setenv("AITOS_TUNING_SCANNER_MAX_DIRECTIONAL_CANCEL_PRESSURE", "0.55")
    monkeypatch.setenv("AITOS_TUNING_POSITIONS_DEAD_CANDLE_EXIT_BARS", "12")
    monkeypatch.setenv("AITOS_TUNING_POSITIONS_STALE_POSITION_MAX_HOURS", "4.0")
    settings = load_tuning_settings(profile="balanced", mode="paper")
    assert settings.scanner.min_liquidity_quality == 5.5
    assert settings.scanner.max_directional_cancel_pressure == 0.55
    assert settings.positions.dead_candle_exit_bars == 12
    assert settings.positions.stale_position_max_hours == 4.0


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
    assert load_tuning_settings(
        profile="balanced", mode="paper"
    ).runtime.config_reload_enabled
    monkeypatch.setenv("AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED"):
        load_tuning_settings(profile="balanced", mode="paper")


def test_intensive_exit_analysis_cadence_is_fair_per_position():
    from aitos.trading.lifecycle import TradeLifecycle

    base = load_tuning_settings(profile="balanced", mode="paper")
    positions = base.positions.model_copy(
        update={"monitoring_interval_seconds": 60, "intensive_analysis_budget": 1}
    )
    lifecycle = object.__new__(TradeLifecycle)
    lifecycle._tuning = base.model_copy(update={"positions": positions})
    lifecycle._last_intensive_analysis_at = {}

    assert lifecycle._allow_intensive_analysis("trade-a") is True
    assert lifecycle._allow_intensive_analysis("trade-a") is False
    # The legacy global budget no longer starves a second open position.
    assert lifecycle._allow_intensive_analysis("trade-b") is True
    # Stop/TP boundary events bypass the regular per-position cadence.
    assert lifecycle._allow_intensive_analysis("trade-a", force=True) is True


def test_emergency_stop_multiple_is_validated_and_configurable(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_POSITIONS_EMERGENCY_STOP_R_MULTIPLE", "2.5")
    settings = load_tuning_settings(profile="balanced", mode="paper")
    assert settings.positions.emergency_stop_r_multiple == 2.5

    monkeypatch.setenv("AITOS_TUNING_POSITIONS_EMERGENCY_STOP_R_MULTIPLE", "1.0")
    with pytest.raises(ValidationError):
        load_tuning_settings(profile="balanced", mode="paper")


def test_hardware_storage_and_telemetry_profiles_are_resolved():
    low = load_tuning_settings(profile="low_resource", mode="paper")
    high = load_tuning_settings(profile="high_performance", mode="paper")
    assert low.hardware.cpu_worker_budget < high.hardware.cpu_worker_budget
    assert low.hardware.memory_reserve_pct > high.hardware.memory_reserve_pct
    assert low.storage.clickhouse_batch_size < high.storage.clickhouse_batch_size
    assert (
        low.storage.historical_backfill_concurrency
        < high.storage.historical_backfill_concurrency
    )
    assert low.telemetry.trace_sample_rate < high.telemetry.trace_sample_rate


def test_liquidity_and_dead_candle_thresholds_reject_invalid_values(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY", "10.1")
    with pytest.raises(ValidationError):
        load_tuning_settings(profile="balanced", mode="paper")

    monkeypatch.delenv("AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY")
    monkeypatch.setenv("AITOS_TUNING_POSITIONS_STALE_POSITION_MAX_HOURS", "0")
    with pytest.raises(ValidationError):
        load_tuning_settings(profile="balanced", mode="paper")


def test_hardware_and_storage_environment_overrides_are_validated(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_HARDWARE_CPU_WORKER_BUDGET", "6")
    monkeypatch.setenv("AITOS_TUNING_STORAGE_CLICKHOUSE_BATCH_SIZE", "2000")
    settings = load_tuning_settings(profile="balanced", mode="paper")
    assert settings.hardware.cpu_worker_budget == 6
    assert settings.storage.clickhouse_batch_size == 2000

    monkeypatch.setenv("AITOS_TUNING_HARDWARE_CPU_WORKER_BUDGET", "0")
    with pytest.raises(ValidationError):
        load_tuning_settings(profile="balanced", mode="paper")


def test_live_mode_rejects_adaptive_tuning(monkeypatch):
    monkeypatch.setenv("AITOS_TUNING_RUNTIME_ADAPTIVE_TUNING_ENABLED", "true")
    with pytest.raises(
        ValidationError, match="adaptive tuning is not permitted in live mode"
    ):
        load_tuning_settings(profile="balanced", mode="live")


def test_risk_ceilings_are_centralized_and_cannot_be_relaxed(monkeypatch):
    settings = load_tuning_settings(profile="high_performance", mode="paper")
    assert settings.risk.max_risk_per_trade_pct == 1.0
    assert settings.risk.max_leverage == 10.0
    monkeypatch.setenv("AITOS_TUNING_RISK_MAX_RISK_PER_TRADE_PCT", "0.5")
    assert (
        load_tuning_settings(
            profile="balanced", mode="paper"
        ).risk.max_risk_per_trade_pct
        == 0.5
    )
    monkeypatch.setenv("AITOS_TUNING_RISK_MAX_RISK_PER_TRADE_PCT", "1.5")
    with pytest.raises(ValidationError):
        load_tuning_settings(profile="balanced", mode="paper")
