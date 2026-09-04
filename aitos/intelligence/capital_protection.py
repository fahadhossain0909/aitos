"""Portfolio-level capital protection policies."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from threading import RLock


@dataclass(frozen=True)
class ProtectionConfig:
    max_portfolio_risk_pct: float = 5.0
    max_correlated_risk_pct: float = 2.5
    max_single_cluster_pct: float = 50.0
    unknown_correlation: float = 0.75
    high_correlation: float = 0.80
    drawdown_1_pct: float = 3.0
    drawdown_2_pct: float = 5.0
    drawdown_3_pct: float = 8.0
    drawdown_stop_pct: float = 10.0
    drawdown_mult_1: float = 0.75
    drawdown_mult_2: float = 0.50
    drawdown_mult_3: float = 0.25
    volatility_mult_high: float = 0.50
    volatility_mult_extreme: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "max_portfolio_risk_pct", "max_correlated_risk_pct", "max_single_cluster_pct",
            "drawdown_1_pct", "drawdown_2_pct", "drawdown_3_pct", "drawdown_stop_pct",
            "drawdown_mult_1", "drawdown_mult_2", "drawdown_mult_3",
            "volatility_mult_high", "volatility_mult_extreme",
        ):
            value = float(getattr(self, name))
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not 0.0 <= self.unknown_correlation <= 1.0:
            raise ValueError("unknown_correlation must be between 0 and 1")
        if not 0.0 <= self.high_correlation <= 1.0:
            raise ValueError("high_correlation must be between 0 and 1")
        if not self.drawdown_1_pct <= self.drawdown_2_pct <= self.drawdown_3_pct <= self.drawdown_stop_pct:
            raise ValueError("drawdown thresholds must be ordered")


@dataclass(frozen=True)
class PortfolioRiskSnapshot:
    equity_usd: float
    equity_peak_usd: float
    position_risk_pct: dict[str, float]
    correlations: dict[tuple[str, str], float]

    @property
    def total_risk_pct(self) -> float:
        return sum(max(0.0, float(v)) for v in self.position_risk_pct.values())

    @property
    def drawdown_pct(self) -> float:
        if self.equity_peak_usd <= 0:
            return 100.0
        return max(0.0, (self.equity_peak_usd - self.equity_usd) / self.equity_peak_usd * 100.0)

    def correlation(self, a: str, b: str, unknown: float) -> float:
        if a == b:
            return 1.0
        value = self.correlations.get((a, b), self.correlations.get((b, a), unknown))
        if not isfinite(value):
            return unknown
        return max(-1.0, min(1.0, value))


@dataclass(frozen=True)
class ProtectionDecision:
    allowed: bool
    risk_multiplier: float
    requested_risk_pct: float
    allowed_risk_pct: float
    correlated_risk_pct: float
    drawdown_pct: float
    reason: str


class PortfolioProtection:
    def __init__(self, config: ProtectionConfig | None = None) -> None:
        self.config = config or ProtectionConfig()

    def drawdown_multiplier(self, drawdown_pct: float) -> float:
        c = self.config
        if drawdown_pct >= c.drawdown_stop_pct:
            return 0.0
        if drawdown_pct >= c.drawdown_3_pct:
            return c.drawdown_mult_3
        if drawdown_pct >= c.drawdown_2_pct:
            return c.drawdown_mult_2
        if drawdown_pct >= c.drawdown_1_pct:
            return c.drawdown_mult_1
        return 1.0

    @staticmethod
    def regime_multiplier(regime: str | None, volatility_score: float | None) -> float:
        volatility = 0.0 if volatility_score is None else max(0.0, min(1.0, float(volatility_score)))
        if volatility >= 0.90:
            return 0.25
        if volatility >= 0.75:
            return 0.50
        normalized = (regime or "").lower()
        if normalized in {"high_volatility", "risk_off"}:
            return 0.50
        if normalized == "transition":
            return 0.75
        return 1.0

    def evaluate(
        self, *, symbol: str, requested_risk_pct: float, snapshot: PortfolioRiskSnapshot,
        regime: str | None = None, volatility_score: float | None = None,
    ) -> ProtectionDecision:
        if requested_risk_pct <= 0 or not isfinite(requested_risk_pct):
            return ProtectionDecision(False, 0.0, requested_risk_pct, 0.0, snapshot.total_risk_pct, snapshot.drawdown_pct, "invalid_requested_risk")
        dd_mult = self.drawdown_multiplier(snapshot.drawdown_pct)
        regime_mult = self.regime_multiplier(regime, volatility_score)
        multiplier = min(dd_mult, regime_mult)
        if multiplier <= 0:
            return ProtectionDecision(False, 0.0, requested_risk_pct, 0.0, snapshot.total_risk_pct, snapshot.drawdown_pct, "drawdown_protection_stop")
        existing_correlated = 0.0
        for other, risk in snapshot.position_risk_pct.items():
            existing_correlated += max(0.0, risk) * abs(snapshot.correlation(symbol, other, self.config.unknown_correlation))
        correlated_risk = existing_correlated + requested_risk_pct * multiplier
        portfolio_remaining = max(0.0, self.config.max_portfolio_risk_pct - snapshot.total_risk_pct)
        correlated_remaining = max(0.0, self.config.max_correlated_risk_pct - existing_correlated)
        allowed_risk = min(requested_risk_pct * multiplier, portfolio_remaining, correlated_remaining)
        allowed_risk = max(0.0, round(allowed_risk, 12))
        if allowed_risk <= 0:
            return ProtectionDecision(False, multiplier, requested_risk_pct, 0.0, correlated_risk, snapshot.drawdown_pct, "portfolio_correlation_or_risk_limit")
        return ProtectionDecision(True, multiplier, requested_risk_pct, allowed_risk, correlated_risk, snapshot.drawdown_pct, "approved")


@dataclass(frozen=True)
class Reservation:
    symbol: str
    capital_usd: float
    risk_budget_usd: float


class CapitalReservation:
    def __init__(self) -> None:
        self._lock = RLock()
        self._reservations: dict[str, Reservation] = {}

    def reserve(self, reservation: Reservation, *, available_capital_usd: float, available_risk_usd: float) -> bool:
        if reservation.capital_usd < 0 or reservation.risk_budget_usd < 0:
            raise ValueError("reservation values must be non-negative")
        with self._lock:
            previous = self._reservations.get(reservation.symbol)
            released_capital = previous.capital_usd if previous else 0.0
            released_risk = previous.risk_budget_usd if previous else 0.0
            if reservation.capital_usd > available_capital_usd + released_capital:
                return False
            if reservation.risk_budget_usd > available_risk_usd + released_risk:
                return False
            self._reservations[reservation.symbol] = reservation
            return True

    def release(self, symbol: str) -> Reservation | None:
        with self._lock:
            return self._reservations.pop(symbol, None)

    def get(self, symbol: str) -> Reservation | None:
        with self._lock:
            return self._reservations.get(symbol)

    @property
    def reserved_capital_usd(self) -> float:
        with self._lock:
            return sum(item.capital_usd for item in self._reservations.values())

    @property
    def reserved_risk_usd(self) -> float:
        with self._lock:
            return sum(item.risk_budget_usd for item in self._reservations.values())

    def snapshot(self) -> tuple[Reservation, ...]:
        with self._lock:
            return tuple(self._reservations.values())
