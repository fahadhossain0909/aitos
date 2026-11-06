from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aitos.intelligence.capital_protection import (
    PortfolioProtection,
    PortfolioRiskSnapshot,
    ProtectionConfig,
)
from aitos.intelligence.exit_intelligence.engine import ExitIntelligenceEngine


def test_exit_ere_is_probability_weighted_net_of_costs_and_auditable():
    engine = ExitIntelligenceEngine()
    plan = SimpleNamespace(
        upside=(SimpleNamespace(price=105.0, probability=0.6),),
        downside=(SimpleNamespace(price=95.0, probability=0.4),),
    )
    edge, reasons, diagnostics = engine._path_and_ere(
        "LONG",
        100.0,
        plan,
        0.001,
    )
    assert edge == pytest.approx(0.009)
    assert diagnostics["path_expected_gain_fraction"] == pytest.approx(0.03)
    assert diagnostics["path_expected_loss_fraction"] == pytest.approx(0.02)
    assert diagnostics["path_transaction_cost_fraction"] == pytest.approx(0.001)


def test_exit_ere_mirrors_directional_path_probabilities_for_short():
    engine = ExitIntelligenceEngine()
    long_plan = SimpleNamespace(
        upside=(SimpleNamespace(price=105.0, probability=0.7),),
        downside=(SimpleNamespace(price=95.0, probability=0.3),),
    )
    short_plan = SimpleNamespace(
        upside=(SimpleNamespace(price=105.0, probability=0.3),),
        downside=(SimpleNamespace(price=95.0, probability=0.7),),
    )
    long_edge, _, _ = engine._path_and_ere("LONG", 100.0, long_plan, 0.001)
    short_edge, _, _ = engine._path_and_ere("SHORT", 100.0, short_plan, 0.001)
    assert long_edge == pytest.approx(short_edge)


def test_correlation_aware_portfolio_protection_caps_incremental_risk():
    protection = PortfolioProtection(
        ProtectionConfig(
            max_portfolio_risk_pct=5.0,
            max_correlated_risk_pct=1.5,
            unknown_correlation=0.9,
        )
    )
    snapshot = PortfolioRiskSnapshot(
        equity_usd=10_000.0,
        equity_peak_usd=10_000.0,
        position_risk_pct={"BTCUSDT": 1.0},
        correlations={("BTCUSDT", "ETHUSDT"): 0.9},
    )
    decision = protection.evaluate(
        symbol="ETHUSDT",
        requested_risk_pct=1.0,
        snapshot=snapshot,
    )
    assert decision.allowed
    assert decision.allowed_risk_pct == pytest.approx(0.6)
    assert decision.correlated_risk_pct == pytest.approx(1.9)
