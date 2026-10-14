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
        TuningSettings.model_validate({
            "profile": "balanced",
            "mode": "paper",
            "scanner": {"universe_mode": "allowlist", "symbol_allowlist": []},
        })


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
