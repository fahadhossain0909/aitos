"""RiskEngine — spec section 31.

The engine scores portfolio, market and system risk and owns the circuit
breaker. Sector exposure is a pre-trade enforced limit: projected sector
breaches must be rejected before an order is submitted, while the configured
hard cap remains the threshold that can independently trigger an emergency
stop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from aitos.core.contracts import (
    AITOSModule,
    Event,
    EventPriority,
    EventResponse,
    HealthStatus,
    ModuleStatus,
)
from aitos.core.exceptions import ModuleNotInitializedError
from aitos.eventbus.redis_bus import EventBus
from aitos.logging_setup import get_logger
from aitos.risk.circuit_breaker import CircuitBreaker
from aitos.risk.models import (
    CircuitBreakerState,
    LimitBreach,
    PortfolioState,
    RiskAction,
    RiskLimits,
    RiskScoreBreakdown,
)

logger = get_logger("aitos.risk.engine")

SCORE_REDUCE_SIZE_THRESHOLD = 70.0
SCORE_NO_NEW_ENTRIES_THRESHOLD = 85.0
SCORE_EMERGENCY_STOP_THRESHOLD = 95.0

COMPONENT_WEIGHTS = {
    "position_risk": 0.30,
    "market_risk": 0.25,
    "system_risk": 0.15,
    "portfolio_risk": 0.30,
}


def _pct_of_limit(value: float, limit: float) -> float:
    if limit <= 0:
        return 100.0
    return max(0.0, min(value / limit * 100.0, 100.0))


def score_position_risk(
    portfolio: PortfolioState, limits: RiskLimits
) -> tuple[float, list[str]]:
    notes: list[str] = []
    leverage_score = _pct_of_limit(portfolio.max_position_leverage, limits.max_leverage)
    if leverage_score > 80:
        notes.append(
            f"leverage utilization high ({portfolio.max_position_leverage:.1f}x / {limits.max_leverage:.1f}x cap)"
        )
    concentration_score = _pct_of_limit(
        len(portfolio.positions), limits.max_open_positions
    )
    largest_position_pct = (
        max((p.notional_usd for p in portfolio.positions), default=0.0)
        / portfolio.equity_usd
        * 100
        if portfolio.equity_usd > 0
        else 0.0
    )
    concentration_score = max(
        concentration_score,
        _pct_of_limit(largest_position_pct, limits.max_sector_exposure_pct),
    )
    return round(leverage_score * 0.6 + concentration_score * 0.4, 2), notes


def score_market_risk(
    portfolio: PortfolioState, limits: RiskLimits
) -> tuple[float, list[str]]:
    notes: list[str] = []
    vol_score = max(0.0, min(portfolio.volatility_percentile, 100.0))
    regime_penalty = {
        "normal": 0.0,
        "trending": 10.0,
        "volatile": 35.0,
        "crisis": 70.0,
    }.get(portfolio.regime, 20.0)
    if regime_penalty >= 35.0:
        notes.append(f"regime is '{portfolio.regime}'")
    correlation_score = _pct_of_limit(
        portfolio.max_pairwise_correlation * 100, limits.max_correlated_exposure_pct * 4
    )
    if portfolio.max_pairwise_correlation > 0.8:
        notes.append(
            f"max pairwise correlation high ({portfolio.max_pairwise_correlation:.2f})"
        )
    return (
        round(
            min(100.0, vol_score * 0.5 + regime_penalty + correlation_score * 0.2), 2
        ),
        notes,
    )


def score_system_risk(
    portfolio: PortfolioState, limits: RiskLimits
) -> tuple[float, list[str]]:
    notes: list[str] = []
    error_score = _pct_of_limit(portfolio.api_error_rate_pct, 5.0)
    if portfolio.api_error_rate_pct > 5.0:
        notes.append(f"API error rate elevated ({portfolio.api_error_rate_pct:.1f}%)")
    latency_score = _pct_of_limit(portfolio.api_latency_ms, 2000.0)
    freshness_score = _pct_of_limit(
        portfolio.data_freshness_seconds, limits.min_data_freshness_hard_cap_seconds
    )
    if portfolio.data_freshness_seconds > limits.min_data_freshness_seconds:
        notes.append(f"market data stale ({portfolio.data_freshness_seconds:.1f}s)")
    accuracy_score = (
        max(0.0, (0.6 - portfolio.model_accuracy) / 0.6 * 100)
        if portfolio.model_accuracy < 0.6
        else 0.0
    )
    if accuracy_score > 0:
        notes.append(f"model accuracy degraded ({portfolio.model_accuracy:.2f})")
    return (
        round(
            min(
                error_score * 0.35
                + latency_score * 0.15
                + freshness_score * 0.2
                + accuracy_score * 0.3,
                100.0,
            ),
            2,
        ),
        notes,
    )


def score_portfolio_risk(
    portfolio: PortfolioState, limits: RiskLimits
) -> tuple[float, list[str]]:
    notes: list[str] = []
    drawdown_score = _pct_of_limit(
        portfolio.current_drawdown_pct, limits.max_drawdown_pct
    )
    if portfolio.current_drawdown_pct > limits.max_drawdown_pct * 0.7:
        notes.append(
            f"drawdown approaching limit ({portfolio.current_drawdown_pct:.1f}% / {limits.max_drawdown_pct:.1f}%)"
        )
    daily_loss_score = _pct_of_limit(
        max(0.0, -portfolio.daily_pnl_pct), limits.max_risk_per_day_pct
    )
    weekly_loss_score = _pct_of_limit(
        max(0.0, -portfolio.weekly_pnl_pct), limits.max_risk_per_week_pct
    )
    sector_exposures = portfolio.sector_exposure_pct
    max_sector_score = max(
        (
            _pct_of_limit(pct, limits.max_sector_exposure_pct)
            for pct in sector_exposures.values()
        ),
        default=0.0,
    )
    if max_sector_score > 80:
        notes.append("sector exposure concentrated")
    return (
        round(
            min(
                drawdown_score * 0.4
                + daily_loss_score * 0.2
                + weekly_loss_score * 0.2
                + max_sector_score * 0.2,
                100.0,
            ),
            2,
        ),
        notes,
    )


def _action_for_score(total: float) -> RiskAction:
    if total > SCORE_EMERGENCY_STOP_THRESHOLD:
        return RiskAction.EMERGENCY_STOP
    if total > SCORE_NO_NEW_ENTRIES_THRESHOLD:
        return RiskAction.NO_NEW_ENTRIES
    if total > SCORE_REDUCE_SIZE_THRESHOLD:
        return RiskAction.REDUCE_SIZE
    return RiskAction.NORMAL


def check_limits(portfolio: PortfolioState, limits: RiskLimits) -> list[LimitBreach]:
    """Return configured-limit breaches and absolute hard-cap breaches.

    Sector default-limit breaches are deliberately marked as blocking
    breaches so TradeLifecycle cannot submit a projected sector overage.
    ``RiskEngine.assess`` separately checks the 40% absolute hard cap before
    deciding whether to trip the emergency circuit breaker.
    """
    breaches: list[LimitBreach] = []

    def _check(
        name: str, observed: float, default_limit: float, hard_cap: float
    ) -> None:
        if observed > hard_cap:
            breaches.append(
                LimitBreach(
                    name,
                    hard_cap,
                    observed,
                    True,
                    f"{name} breached HARD CAP: {observed} > {hard_cap}",
                )
            )
        elif observed > default_limit:
            breaches.append(
                LimitBreach(
                    name,
                    default_limit,
                    observed,
                    False,
                    f"{name} breached default limit: {observed} > {default_limit}",
                )
            )

    _check(
        "max_drawdown_pct",
        portfolio.current_drawdown_pct,
        limits.max_drawdown_pct,
        limits.max_drawdown_hard_cap_pct,
    )
    _check(
        "max_leverage",
        portfolio.max_position_leverage,
        limits.max_leverage,
        limits.max_leverage_hard_cap,
    )
    _check(
        "max_open_positions",
        len(portfolio.positions),
        limits.max_open_positions,
        limits.max_open_positions_hard_cap,
    )
    _check(
        "max_risk_per_day_pct",
        max(0.0, -portfolio.daily_pnl_pct),
        limits.max_risk_per_day_pct,
        limits.max_risk_per_day_hard_cap_pct,
    )
    _check(
        "max_risk_per_week_pct",
        max(0.0, -portfolio.weekly_pnl_pct),
        limits.max_risk_per_week_pct,
        limits.max_risk_per_week_hard_cap_pct,
    )
    _check(
        "max_correlated_exposure_pct",
        portfolio.max_pairwise_correlation * 100,
        limits.max_correlated_exposure_pct,
        limits.max_correlated_exposure_hard_cap_pct,
    )
    for sector, pct in portfolio.sector_exposure_pct.items():
        _check(
            f"max_sector_exposure_pct[{sector}]",
            pct,
            limits.max_sector_exposure_pct,
            limits.max_sector_exposure_hard_cap_pct,
        )
    _check(
        "min_data_freshness_seconds",
        portfolio.data_freshness_seconds,
        limits.min_data_freshness_seconds,
        limits.min_data_freshness_hard_cap_seconds,
    )
    return breaches


def _absolute_hard_cap_breaches(
    portfolio: PortfolioState, limits: RiskLimits
) -> list[LimitBreach]:
    """Return only breaches of the configured absolute hard caps."""
    breaches: list[LimitBreach] = []
    checks = [
        (
            "max_drawdown_pct",
            portfolio.current_drawdown_pct,
            limits.max_drawdown_hard_cap_pct,
        ),
        ("max_leverage", portfolio.max_position_leverage, limits.max_leverage_hard_cap),
        (
            "max_open_positions",
            len(portfolio.positions),
            limits.max_open_positions_hard_cap,
        ),
        (
            "max_risk_per_day_pct",
            max(0.0, -portfolio.daily_pnl_pct),
            limits.max_risk_per_day_hard_cap_pct,
        ),
        (
            "max_risk_per_week_pct",
            max(0.0, -portfolio.weekly_pnl_pct),
            limits.max_risk_per_week_hard_cap_pct,
        ),
        (
            "max_correlated_exposure_pct",
            portfolio.max_pairwise_correlation * 100,
            limits.max_correlated_exposure_hard_cap_pct,
        ),
        (
            "min_data_freshness_seconds",
            portfolio.data_freshness_seconds,
            limits.min_data_freshness_hard_cap_seconds,
        ),
    ]
    for name, observed, cap in checks:
        if observed > cap:
            breaches.append(
                LimitBreach(
                    name,
                    cap,
                    observed,
                    True,
                    f"{name} breached HARD CAP: {observed} > {cap}",
                )
            )
    for sector, pct in portfolio.sector_exposure_pct.items():
        if pct > limits.max_sector_exposure_hard_cap_pct:
            breaches.append(
                LimitBreach(
                    f"max_sector_exposure_pct[{sector}]",
                    limits.max_sector_exposure_hard_cap_pct,
                    pct,
                    True,
                    f"max_sector_exposure_pct[{sector}] breached HARD CAP: {pct} > {limits.max_sector_exposure_hard_cap_pct}",
                )
            )
    return breaches


class RiskEngine(AITOSModule):
    def __init__(
        self,
        event_bus: EventBus,
        limits: RiskLimits | None = None,
        circuit_breaker_cooldown_seconds: float = 300.0,
    ) -> None:
        self._event_bus = event_bus
        self._limits = limits or RiskLimits()
        self._circuit_breaker = CircuitBreaker(
            cooldown_seconds=circuit_breaker_cooldown_seconds
        )
        self._initialized = False
        self._last_assessment: RiskScoreBreakdown | None = None
        self._last_event_time: str | None = None

    @property
    def module_id(self) -> str:
        return "risk-engine"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def initialize(self, config: dict[str, Any]) -> None:
        if self._initialized:
            return
        self._initialized = True
        logger.info(
            "RiskEngine initialized",
            extra={"aitos_extra": {"limits": self._limits.model_dump()}},
        )

    async def health_check(self) -> HealthStatus:
        status = ModuleStatus.HEALTHY
        if self._circuit_breaker.state == CircuitBreakerState.OPEN:
            status = ModuleStatus.UNHEALTHY
        elif self._circuit_breaker.state == CircuitBreakerState.HALF_OPEN:
            status = ModuleStatus.DEGRADED
        return HealthStatus(
            module_id=self.module_id,
            status=status,
            latency_ms=0.0,
            last_event_time=self._last_event_time,
            details={
                "circuit_breaker_state": self._circuit_breaker.state.value,
                "last_score": (
                    self._last_assessment.total if self._last_assessment else None
                ),
            },
        )

    async def shutdown(self, grace_period_seconds: float = 30.0) -> None:
        logger.info("RiskEngine shut down")

    async def emit_events(self) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover

    async def handle_event(self, event: Event) -> EventResponse | None:
        return None

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def last_assessment(self) -> RiskScoreBreakdown | None:
        return self._last_assessment

    async def assess(self, portfolio: PortfolioState) -> RiskScoreBreakdown:
        self._require_initialized()
        position_risk, position_notes = score_position_risk(portfolio, self._limits)
        market_risk, market_notes = score_market_risk(portfolio, self._limits)
        system_risk, system_notes = score_system_risk(portfolio, self._limits)
        portfolio_risk, portfolio_notes = score_portfolio_risk(portfolio, self._limits)
        total = (
            position_risk * COMPONENT_WEIGHTS["position_risk"]
            + market_risk * COMPONENT_WEIGHTS["market_risk"]
            + system_risk * COMPONENT_WEIGHTS["system_risk"]
            + portfolio_risk * COMPONENT_WEIGHTS["portfolio_risk"]
        )
        action = _action_for_score(total)
        breakdown = RiskScoreBreakdown(
            position_risk=position_risk,
            market_risk=market_risk,
            system_risk=system_risk,
            portfolio_risk=portfolio_risk,
            total=round(total, 2),
            action=action,
            explanation=position_notes + market_notes + system_notes + portfolio_notes,
        )
        self._last_assessment = breakdown
        await self._event_bus.publish(
            Event(
                topic="risk.score_update",
                payload=breakdown.to_dict(),
                source_module=self.module_id,
            )
        )
        self._last_event_time = breakdown.computed_at
        hard_breaches = _absolute_hard_cap_breaches(portfolio, self._limits)
        if action == RiskAction.EMERGENCY_STOP or hard_breaches:
            reason = (
                f"risk score {total:.1f} > {SCORE_EMERGENCY_STOP_THRESHOLD}"
                if action == RiskAction.EMERGENCY_STOP
                else f"hard cap breach: {hard_breaches[0].message}"
            )
            await self.trigger_emergency_stop(reason)
        return breakdown

    def check_limits(self, portfolio: PortfolioState) -> list[LimitBreach]:
        self._require_initialized()
        return check_limits(portfolio, self._limits)

    async def trigger_emergency_stop(self, reason: str) -> None:
        self._require_initialized()
        self._circuit_breaker.trip(reason)
        await self._event_bus.publish(
            Event(
                topic="risk.emergency_stop",
                payload={
                    "reason": reason,
                    "circuit_breaker_state": self._circuit_breaker.state.value,
                },
                source_module=self.module_id,
                priority=EventPriority.CRITICAL,
            )
        )
        logger.error(
            "EMERGENCY STOP triggered", extra={"aitos_extra": {"reason": reason}}
        )

    async def attempt_recovery(self) -> bool:
        self._require_initialized()
        transitioned = self._circuit_breaker.attempt_half_open()
        if transitioned:
            await self._event_bus.publish(
                Event(
                    topic="risk.circuit_breaker",
                    payload={
                        "state": self._circuit_breaker.state.value,
                        "reason": "cooldown elapsed",
                    },
                    source_module=self.module_id,
                    priority=EventPriority.HIGH,
                )
            )
        return transitioned

    def veto(self, portfolio: PortfolioState | None = None) -> tuple[bool, str]:
        if not self._circuit_breaker.is_trading_allowed():
            return True, f"circuit breaker is {self._circuit_breaker.state.value}"
        if self._last_assessment is not None and self._last_assessment.action in (
            RiskAction.NO_NEW_ENTRIES,
            RiskAction.EMERGENCY_STOP,
        ):
            return (
                True,
                f"risk score {self._last_assessment.total} triggers {self._last_assessment.action.value}",
            )
        return False, "within limits"

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ModuleNotInitializedError(
                "RiskEngine.initialize() must be called first"
            )
