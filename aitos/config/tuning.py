"""Validated centralized runtime-tuning configuration for AITOS.

Profiles are capacity hints, not trading-risk recommendations. Live risk
limits remain owned by the risk engine; adaptive tuning/reload are opt-in.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aitos.config.paths import get_project_root

ProfileName = Literal["low_resource", "balanced", "high_performance"]
TradingMode = Literal["paper", "live"]
UniverseMode = Literal["all_eligible", "allowlist"]


class ScannerTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    top_n: int = Field(default=5, ge=1, le=1000)
    universe_mode: UniverseMode = "all_eligible"
    universe_limit: int = Field(default=100, ge=1, le=5000)
    refresh_interval_seconds: float = Field(default=10.0, gt=0, le=3600)
    symbol_allowlist: tuple[str, ...] = ()


class AnalysisTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expensive_analysis_limit: int = Field(default=5, ge=1, le=5000)
    expensive_analysis_concurrency: int = Field(default=2, ge=1, le=256)
    lightweight_concurrency: int = Field(default=8, ge=1, le=512)
    timeout_seconds: float = Field(default=5.0, gt=0, le=300)


class EventBusTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    consumer_concurrency: int = Field(default=8, ge=1, le=32)
    connection_pool_size: int = Field(default=256, ge=8, le=4096)
    consumer_batch_size: int = Field(default=100, ge=1, le=1000)


class PositionTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_open_positions: int = Field(default=3, ge=1, le=100)
    max_positions_per_symbol: int = Field(default=1, ge=1, le=10)
    monitoring_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    intensive_analysis_budget: int = Field(default=2, ge=0, le=100)


class RuntimeTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    adaptive_tuning_enabled: bool = False
    config_reload_enabled: bool = False


class TuningSettings(BaseModel):
    """Immutable, validated snapshot of effective tuning values."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: ProfileName = "balanced"
    mode: TradingMode = "paper"
    scanner: ScannerTuning = Field(default_factory=ScannerTuning)
    analysis: AnalysisTuning = Field(default_factory=AnalysisTuning)
    event_bus: EventBusTuning = Field(default_factory=EventBusTuning)
    positions: PositionTuning = Field(default_factory=PositionTuning)
    runtime: RuntimeTuning = Field(default_factory=RuntimeTuning)

    @model_validator(mode="after")
    def validate_allowlist(self) -> "TuningSettings":
        if (
            self.scanner.universe_mode == "allowlist"
            and not self.scanner.symbol_allowlist
        ):
            raise ValueError(
                "scanner.symbol_allowlist must be non-empty when "
                "universe_mode='allowlist'"
            )
        return self


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _profiles_path() -> Path:
    return get_project_root() / "aitos" / "config" / "tuning_profiles.json"


@lru_cache(maxsize=1)
def _read_profiles() -> dict[str, Any]:
    path = _profiles_path()
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Cannot load AITOS tuning profiles from {path}: {exc}"
        ) from exc
    if payload.get("version") != 1 or not isinstance(payload.get("profiles"), dict):
        raise RuntimeError(f"Unsupported or malformed AITOS tuning profiles: {path}")
    return payload


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("expected one of: true/false, yes/no, on/off, 1/0")


def _env_overrides() -> dict[str, Any]:
    """Read a whitelist of environment overrides; ignore unrelated variables."""
    mapping: dict[str, tuple[str, str, Any]] = {
        "AITOS_TUNING_SCANNER_TOP_N": ("scanner", "top_n", int),
        "AITOS_TUNING_SCANNER_UNIVERSE_MODE": ("scanner", "universe_mode", str),
        "AITOS_TUNING_SCANNER_UNIVERSE_LIMIT": ("scanner", "universe_limit", int),
        "AITOS_TUNING_SCANNER_REFRESH_INTERVAL_SECONDS": (
            "scanner",
            "refresh_interval_seconds",
            float,
        ),
        "AITOS_TUNING_SCANNER_SYMBOL_ALLOWLIST": (
            "scanner",
            "symbol_allowlist",
            lambda v: tuple(
                symbol.strip().upper() for symbol in v.split(",") if symbol.strip()
            ),
        ),
        "AITOS_TUNING_ANALYSIS_EXPENSIVE_ANALYSIS_LIMIT": (
            "analysis",
            "expensive_analysis_limit",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_EXPENSIVE_ANALYSIS_CONCURRENCY": (
            "analysis",
            "expensive_analysis_concurrency",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_LIGHTWEIGHT_CONCURRENCY": (
            "analysis",
            "lightweight_concurrency",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_TIMEOUT_SECONDS": ("analysis", "timeout_seconds", float),
        "AITOS_TUNING_EVENT_BUS_CONSUMER_CONCURRENCY": (
            "event_bus",
            "consumer_concurrency",
            int,
        ),
        "AITOS_TUNING_EVENT_BUS_CONNECTION_POOL_SIZE": (
            "event_bus",
            "connection_pool_size",
            int,
        ),
        "AITOS_TUNING_EVENT_BUS_CONSUMER_BATCH_SIZE": (
            "event_bus",
            "consumer_batch_size",
            int,
        ),
        "AITOS_TUNING_POSITIONS_MAX_OPEN_POSITIONS": (
            "positions",
            "max_open_positions",
            int,
        ),
        "AITOS_TUNING_POSITIONS_MAX_POSITIONS_PER_SYMBOL": (
            "positions",
            "max_positions_per_symbol",
            int,
        ),
        "AITOS_TUNING_POSITIONS_MONITORING_INTERVAL_SECONDS": (
            "positions",
            "monitoring_interval_seconds",
            float,
        ),
        "AITOS_TUNING_POSITIONS_INTENSIVE_ANALYSIS_BUDGET": (
            "positions",
            "intensive_analysis_budget",
            int,
        ),
        "AITOS_TUNING_RUNTIME_ADAPTIVE_TUNING_ENABLED": (
            "runtime",
            "adaptive_tuning_enabled",
            _parse_bool,
        ),
        "AITOS_TUNING_RUNTIME_CONFIG_RELOAD_ENABLED": (
            "runtime",
            "config_reload_enabled",
            _parse_bool,
        ),
    }
    result: dict[str, Any] = {}
    for env_name, (section, key, converter) in mapping.items():
        raw = os.getenv(env_name)
        if raw is not None:
            try:
                value = converter(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid value for {env_name}: {raw!r}") from exc
            result.setdefault(section, {})[key] = value
    return result


def load_tuning_settings(
    *, profile: ProfileName | None = None, mode: TradingMode | None = None
) -> TuningSettings:
    """Resolve profile -> mode overrides -> explicit environment overrides."""
    payload = _read_profiles()
    selected_profile = profile or os.getenv("AITOS_TUNING_PROFILE", "balanced")
    selected_mode = mode or os.getenv("AITOS_MODE", "paper")
    profiles = payload["profiles"]
    if selected_profile not in profiles:
        raise ValueError(f"Unknown AITOS_TUNING_PROFILE: {selected_profile}")
    if selected_mode not in payload.get("modes", {}):
        raise ValueError(f"Unknown AITOS_MODE: {selected_mode}")
    raw = deepcopy(profiles[selected_profile])
    raw = _deep_merge(raw, payload.get("modes", {}).get(selected_mode, {}))
    raw = _deep_merge(raw, _env_overrides())
    try:
        return TuningSettings.model_validate(
            {"profile": selected_profile, "mode": selected_mode, **raw}
        )
    except ValidationError:
        raise



def reload_tuning_settings(
    *, profile: ProfileName | None = None, mode: TradingMode | None = None
) -> TuningSettings:
    """Explicitly reload the profile document and return a newly validated snapshot.

    Callers must deliberately apply the returned snapshot to live components;
    this function never mutates running components behind their back.
    """
    _read_profiles.cache_clear()
    return load_tuning_settings(profile=profile, mode=mode)


def effective_tuning_report(settings: TuningSettings | None = None) -> dict[str, Any]:
    """Return a secret-free, stable audit record for the effective tuning."""
    import hashlib

    resolved = settings or load_tuning_settings()
    payload = resolved.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {
        "profile": resolved.profile,
        "mode": resolved.mode,
        "config_version": hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16],
        "settings": payload,
    }
