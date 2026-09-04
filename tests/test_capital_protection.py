from aitos.intelligence.capital_protection import (
    CapitalReservation,
    PortfolioProtection,
    PortfolioRiskSnapshot,
    Reservation,
)


def snapshot(**kwargs):
    return PortfolioRiskSnapshot(
        equity_usd=10_000.0,
        equity_peak_usd=kwargs.get("peak", 10_000.0),
        position_risk_pct=kwargs.get("positions", {}),
        correlations=kwargs.get("correlations", {}),
    )


def test_drawdown_reduces_risk_and_stops_at_limit():
    gate = PortfolioProtection()
    assert gate.drawdown_multiplier(4.0) == 0.75
    assert gate.drawdown_multiplier(6.0) == 0.50
    assert gate.drawdown_multiplier(9.0) == 0.25
    assert gate.drawdown_multiplier(10.0) == 0.0


def test_high_volatility_reduces_requested_risk():
    decision = PortfolioProtection().evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(),
        regime="high_volatility",
        volatility_score=0.80,
    )
    assert decision.allowed
    assert decision.allowed_risk_pct == 0.50
    assert decision.risk_multiplier == 0.50


def test_correlated_existing_risk_is_not_treated_as_zero():
    decision = PortfolioProtection().evaluate(
        symbol="ETHUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(
            positions={"BTCUSDT": 2.0},
            correlations={("BTCUSDT", "ETHUSDT"): 0.95},
        ),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct <= 0.60
    assert decision.correlated_risk_pct > 2.0


def test_unknown_correlation_is_conservative():
    decision = PortfolioProtection().evaluate(
        symbol="SOLUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(positions={"BTCUSDT": 2.0}),
    )
    assert decision.allowed
    assert decision.allowed_risk_pct <= 1.0
    assert decision.correlated_risk_pct >= 1.5


def test_drawdown_stop_is_hard_veto():
    decision = PortfolioProtection().evaluate(
        symbol="BTCUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot(peak=11_500.0),
    )
    assert not decision.allowed
    assert decision.reason == "drawdown_protection_stop"


def test_reservation_prevents_oversubscription_and_is_idempotent():
    ledger = CapitalReservation()
    first = Reservation("BTCUSDT", 6_000.0, 60.0)
    second = Reservation("ETHUSDT", 5_000.0, 50.0)
    assert ledger.reserve(
        first, available_capital_usd=10_000.0, available_risk_usd=100.0
    )
    assert not ledger.reserve(
        second, available_capital_usd=10_000.0, available_risk_usd=100.0
    )
    assert ledger.reserve(
        first, available_capital_usd=4_000.0, available_risk_usd=40.0
    )
    assert ledger.reserved_capital_usd == 6_000.0
    assert ledger.release("BTCUSDT") == first
    assert ledger.release("BTCUSDT") is None
    assert ledger.reserved_capital_usd == 0.0
