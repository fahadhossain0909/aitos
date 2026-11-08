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

DEFAULT_CORE_FUTURES_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "LTCUSDT",
    "BCHUSDT",
    "AVAXUSDT",
    "DOTUSDT",
    "TRXUSDT",
    "SUIUSDT",
    "TONUSDT",
)


class ScannerTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    top_n: int = Field(default=5, ge=1, le=1000)
    universe_mode: UniverseMode = "all_eligible"
    universe_limit: int = Field(default=100, ge=1, le=5000)
    refresh_interval_seconds: float = Field(default=10.0, gt=0, le=3600)
    symbol_allowlist: tuple[str, ...] = ()
    core_symbols: tuple[str, ...] = DEFAULT_CORE_FUTURES_SYMBOLS
    min_quote_volume_usd: float = Field(default=10_000_000.0, ge=0)
    min_trade_count: int = Field(default=10_000, ge=0)
    min_contract_age_days: int = Field(default=30, ge=0, le=3650)
    dynamic_universe_limit: int = Field(default=35, ge=0, le=5000)
    min_liquidity_quality: float = Field(default=5.0, ge=0, le=10)
    max_directional_cancel_pressure: float = Field(default=0.6, ge=0, le=1)
    htf_filter_enabled: bool = True
    htf_timeframe: str = "auto"
    htf_lookback: int = Field(default=80, ge=30, le=1000)
    htf_min_adx: float = Field(default=25.0, ge=0, le=100)
    htf_min_di_gap: float = Field(default=5.0, ge=0, le=100)
    htf_neutral_min_ltf_score: float = Field(default=6.0, ge=0, le=10)
    htf_countertrend_min_ltf_score: float = Field(default=7.0, ge=0, le=10)
    htf_alignment_balance_threshold: float = Field(default=0.25, gt=0, le=1)
    htf_allow_neutral: bool = True
    htf_request_budget_per_minute: int = Field(default=360, ge=6, le=1200)
    htf_fetch_concurrency: int = Field(default=4, ge=1, le=16)
    htf_freshness_grace_seconds: float = Field(default=15.0, ge=0, le=300)


class AnalysisTuning(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    expensive_analysis_limit: int = Field(default=5, ge=1, le=5000)
    candidate_pool_limit: int = Field(default=20, ge=1, le=5000)
    candidate_batch_size: int = Field(default=5, ge=1, le=500)
    max_candidate_analyses_per_cycle: int = Field(default=20, ge=1, le=5000)
    rejection_cooldown_seconds: float = Field(default=15.0, ge=0, le=3600)
    kernel_rejection_cooldown_seconds: float = Field(default=20.0, ge=0, le=3600)
    market_change_recheck_pct: float = Field(default=0.15, ge=0, le=100)
    ranking_hysteresis_points: float = Field(default=1.0, ge=0, le=10)
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
    # Legacy compatibility field; EIE cadence is now enforced independently
    # for each open position rather than by a global shared budget.
    intensive_analysis_budget: int = Field(default=2, ge=0, le=100)
    emergency_stop_r_multiple: float = Field(default=2.0, gt=1.0, le=10.0)
    max_partial_exit_count: int = Field(default=3, ge=0, le=20)
    partial_exit_cooldown_seconds: float = Field(default=60.0, ge=0, le=86400)
    min_remaining_position_usd: float = Field(default=1.0, ge=0, le=1_000_000)
    dead_candle_exit_bars: int = Field(default=16, ge=1, le=96)
    dead_candle_max_progress_r: float = Field(default=0.10, ge=0, le=1)
    dead_candle_entry_band_pct: float = Field(default=0.002, ge=0, le=0.05)
    stale_position_max_hours: float = Field(default=6.0, gt=0, le=72)


class RiskTuning(BaseModel):
    """Central risk ceilings; capacity profiles cannot raise these hard bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    max_risk_per_trade_pct: float = Field(default=1.0, gt=0, le=1.0)
    max_leverage: float = Field(default=10.0, ge=1.0, le=10.0)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=10.0)
    max_correlated_exposure_pct: float = Field(default=15.0, gt=0, le=15.0)
    max_sector_exposure_pct: float = Field(default=20.0, gt=0, le=20.0)


class HardwareTuning(BaseModel):
    """Capacity guardrails for a host; these do not grant more trading risk."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu_worker_budget: int = Field(default=4, ge=1, le=256)
    memory_budget_mb: int = Field(default=4096, ge=256, le=1_048_576)
    memory_reserve_pct: int = Field(default=20, ge=5, le=80)
    max_background_tasks: int = Field(default=4, ge=0, le=512)


class StorageTuning(BaseModel):
    """Limits for non-live historical/storage work."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    historical_backfill_concurrency: int = Field(default=1, ge=1, le=64)
    clickhouse_batch_size: int = Field(default=1000, ge=1, le=100_000)
    clickhouse_query_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    retention_days: int = Field(default=30, ge=1, le=3650)
    historical_orderbook_levels: int = Field(default=100, ge=1, le=1000)


class TelemetryTuning(BaseModel):
    """Operational observability cadence and sampling."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    health_interval_seconds: float = Field(default=15.0, gt=0, le=3600)
    metrics_sample_interval_seconds: float = Field(default=5.0, gt=0, le=3600)
    trace_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)


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
    risk: RiskTuning = Field(default_factory=RiskTuning)
    hardware: HardwareTuning = Field(default_factory=HardwareTuning)
    storage: StorageTuning = Field(default_factory=StorageTuning)
    telemetry: TelemetryTuning = Field(default_factory=TelemetryTuning)
    runtime: RuntimeTuning = Field(default_factory=RuntimeTuning)

    @model_validator(mode="after")
    def validate_allowlist(self) -> TuningSettings:
        if (
            self.scanner.universe_mode == "allowlist"
            and not self.scanner.symbol_allowlist
        ):
            raise ValueError(
                "scanner.symbol_allowlist must be non-empty when "
                "universe_mode='allowlist'"
            )
        if self.mode == "live" and self.runtime.adaptive_tuning_enabled:
            raise ValueError(
                "adaptive tuning is not permitted in live mode; change settings "
                "only through the approved risk/configuration workflow"
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
        "AITOS_TUNING_SCANNER_MIN_QUOTE_VOLUME_USD": (
            "scanner",
            "min_quote_volume_usd",
            float,
        ),
        "AITOS_TUNING_SCANNER_MIN_TRADE_COUNT": ("scanner", "min_trade_count", int),
        "AITOS_TUNING_SCANNER_MIN_CONTRACT_AGE_DAYS": (
            "scanner",
            "min_contract_age_days",
            int,
        ),
        "AITOS_TUNING_SCANNER_DYNAMIC_UNIVERSE_LIMIT": (
            "scanner",
            "dynamic_universe_limit",
            int,
        ),
        "AITOS_TUNING_SCANNER_MIN_LIQUIDITY_QUALITY": (
            "scanner",
            "min_liquidity_quality",
            float,
        ),
        "AITOS_TUNING_SCANNER_MAX_DIRECTIONAL_CANCEL_PRESSURE": (
            "scanner",
            "max_directional_cancel_pressure",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_FILTER_ENABLED": (
            "scanner",
            "htf_filter_enabled",
            _parse_bool,
        ),
        "AITOS_TUNING_SCANNER_HTF_TIMEFRAME": (
            "scanner",
            "htf_timeframe",
            str,
        ),
        "AITOS_TUNING_SCANNER_HTF_LOOKBACK": (
            "scanner",
            "htf_lookback",
            int,
        ),
        "AITOS_TUNING_SCANNER_HTF_MIN_ADX": (
            "scanner",
            "htf_min_adx",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_MIN_DI_GAP": (
            "scanner",
            "htf_min_di_gap",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_NEUTRAL_MIN_LTF_SCORE": (
            "scanner",
            "htf_neutral_min_ltf_score",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_COUNTERTREND_MIN_LTF_SCORE": (
            "scanner",
            "htf_countertrend_min_ltf_score",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_ALIGNMENT_BALANCE_THRESHOLD": (
            "scanner",
            "htf_alignment_balance_threshold",
            float,
        ),
        "AITOS_TUNING_SCANNER_HTF_ALLOW_NEUTRAL": (
            "scanner",
            "htf_allow_neutral",
            _parse_bool,
        ),
        "AITOS_TUNING_SCANNER_HTF_REQUEST_BUDGET_PER_MINUTE": (
            "scanner",
            "htf_request_budget_per_minute",
            int,
        ),
        "AITOS_TUNING_SCANNER_HTF_FETCH_CONCURRENCY": (
            "scanner",
            "htf_fetch_concurrency",
            int,
        ),
        "AITOS_TUNING_SCANNER_HTF_FRESHNESS_GRACE_SECONDS": (
            "scanner",
            "htf_freshness_grace_seconds",
            float,
        ),
        "AITOS_TUNING_SCANNER_CORE_SYMBOLS": (
            "scanner",
            "core_symbols",
            lambda v: tuple(
                symbol.strip().upper() for symbol in v.split(",") if symbol.strip()
            ),
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
        "AITOS_TUNING_ANALYSIS_CANDIDATE_POOL_LIMIT": (
            "analysis",
            "candidate_pool_limit",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_CANDIDATE_BATCH_SIZE": (
            "analysis",
            "candidate_batch_size",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_MAX_CANDIDATE_ANALYSES_PER_CYCLE": (
            "analysis",
            "max_candidate_analyses_per_cycle",
            int,
        ),
        "AITOS_TUNING_ANALYSIS_REJECTION_COOLDOWN_SECONDS": (
            "analysis",
            "rejection_cooldown_seconds",
            float,
        ),
        "AITOS_TUNING_ANALYSIS_KERNEL_REJECTION_COOLDOWN_SECONDS": (
            "analysis",
            "kernel_rejection_cooldown_seconds",
            float,
        ),
        "AITOS_TUNING_ANALYSIS_MARKET_CHANGE_RECHECK_PCT": (
            "analysis",
            "market_change_recheck_pct",
            float,
        ),
        "AITOS_TUNING_ANALYSIS_RANKING_HYSTERESIS_POINTS": (
            "analysis",
            "ranking_hysteresis_points",
            float,
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
        "AITOS_TUNING_POSITIONS_EMERGENCY_STOP_R_MULTIPLE": (
            "positions",
            "emergency_stop_r_multiple",
            float,
        ),
        "AITOS_TUNING_POSITIONS_MAX_PARTIAL_EXIT_COUNT": (
            "positions",
            "max_partial_exit_count",
            int,
        ),
        "AITOS_TUNING_POSITIONS_PARTIAL_EXIT_COOLDOWN_SECONDS": (
            "positions",
            "partial_exit_cooldown_seconds",
            float,
        ),
        "AITOS_TUNING_POSITIONS_MIN_REMAINING_POSITION_USD": (
            "positions",
            "min_remaining_position_usd",
            float,
        ),
        "AITOS_TUNING_POSITIONS_DEAD_CANDLE_EXIT_BARS": (
            "positions",
            "dead_candle_exit_bars",
            int,
        ),
        "AITOS_TUNING_POSITIONS_DEAD_CANDLE_MAX_PROGRESS_R": (
            "positions",
            "dead_candle_max_progress_r",
            float,
        ),
        "AITOS_TUNING_POSITIONS_DEAD_CANDLE_ENTRY_BAND_PCT": (
            "positions",
            "dead_candle_entry_band_pct",
            float,
        ),
        "AITOS_TUNING_POSITIONS_STALE_POSITION_MAX_HOURS": (
            "positions",
            "stale_position_max_hours",
            float,
        ),
        "AITOS_TUNING_RISK_MAX_RISK_PER_TRADE_PCT": (
            "risk",
            "max_risk_per_trade_pct",
            float,
        ),
        "AITOS_TUNING_RISK_MAX_LEVERAGE": ("risk", "max_leverage", float),
        "AITOS_TUNING_RISK_MAX_DRAWDOWN_PCT": ("risk", "max_drawdown_pct", float),
        "AITOS_TUNING_RISK_MAX_CORRELATED_EXPOSURE_PCT": (
            "risk",
            "max_correlated_exposure_pct",
            float,
        ),
        "AITOS_TUNING_RISK_MAX_SECTOR_EXPOSURE_PCT": (
            "risk",
            "max_sector_exposure_pct",
            float,
        ),
        "AITOS_TUNING_HARDWARE_CPU_WORKER_BUDGET": (
            "hardware",
            "cpu_worker_budget",
            int,
        ),
        "AITOS_TUNING_HARDWARE_MEMORY_BUDGET_MB": ("hardware", "memory_budget_mb", int),
        "AITOS_TUNING_HARDWARE_MEMORY_RESERVE_PCT": (
            "hardware",
            "memory_reserve_pct",
            int,
        ),
        "AITOS_TUNING_HARDWARE_MAX_BACKGROUND_TASKS": (
            "hardware",
            "max_background_tasks",
            int,
        ),
        "AITOS_TUNING_STORAGE_HISTORICAL_BACKFILL_CONCURRENCY": (
            "storage",
            "historical_backfill_concurrency",
            int,
        ),
        "AITOS_TUNING_STORAGE_CLICKHOUSE_BATCH_SIZE": (
            "storage",
            "clickhouse_batch_size",
            int,
        ),
        "AITOS_TUNING_STORAGE_CLICKHOUSE_QUERY_TIMEOUT_SECONDS": (
            "storage",
            "clickhouse_query_timeout_seconds",
            float,
        ),
        "AITOS_TUNING_STORAGE_RETENTION_DAYS": ("storage", "retention_days", int),
        "AITOS_TUNING_STORAGE_HISTORICAL_ORDERBOOK_LEVELS": (
            "storage",
            "historical_orderbook_levels",
            int,
        ),
        "AITOS_TUNING_TELEMETRY_HEALTH_INTERVAL_SECONDS": (
            "telemetry",
            "health_interval_seconds",
            float,
        ),
        "AITOS_TUNING_TELEMETRY_METRICS_SAMPLE_INTERVAL_SECONDS": (
            "telemetry",
            "metrics_sample_interval_seconds",
            float,
        ),
        "AITOS_TUNING_TELEMETRY_TRACE_SAMPLE_RATE": (
            "telemetry",
            "trace_sample_rate",
            float,
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
